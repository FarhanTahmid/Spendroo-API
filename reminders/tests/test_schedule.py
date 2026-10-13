import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from dateutil import rrule
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from reminders import schedule

CASES = json.loads((Path(__file__).parent / 'recurrence_cases.json').read_text())['cases']
DEFAULTS = {
    'freq': 'monthly', 'interval': 1, 'weekday_mask': 0, 'month_day': None, 'set_pos': None, 'week_start': 5,
    'start_on': '2026-10-13', 'end_on': None,
}


def a_schedule(**fields):
    fields = {**DEFAULTS, **fields}
    for name in ('start_on', 'end_on'):
        if isinstance(fields[name], str):
            fields[name] = date.fromisoformat(fields[name])
    return SimpleNamespace(**fields)


def days(found):
    return [day.isoformat() for day in found]


class SharedCasesTests(SimpleTestCase):
    """The file the app's own tests read (test/fixtures/recurrence_cases.json): the two must agree."""

    def test_every_case(self):
        self.assertGreater(len(CASES), 20)
        for case in CASES:
            with self.subTest(case['name']):
                found = schedule.occurrences(a_schedule(**case['rule']), date.fromisoformat(case['from']), len(case['expect']) + 1)
                self.assertEqual(days(found)[:len(case['expect'])], case['expect'])
                schedule.check(a_schedule(**case['rule']))

    def test_the_cases_that_matter_most_by_hand(self):
        expected = {
            # Where dateutil would skip February, April, June...
            'monthly on the 31st: short months use their last day': ['2026-01-31', '2026-02-28', '2026-03-31', '2026-04-30'],
            'first Friday of the month': ['2026-11-06', '2026-12-04', '2027-01-01'],
            # 13 Oct 2026 is a Tuesday. A week from Saturday holds Sat 10 and Tue 13; from Monday, Tue 13 and Sat 17.
            'every 2 weeks on Sat and Tue, weeks starting Saturday': ['2026-10-13', '2026-10-24', '2026-10-27'],
            'every 2 weeks on Sat and Tue, weeks starting Monday': ['2026-10-13', '2026-10-17', '2026-10-27'],
            'yearly on 29 February': ['2024-02-29', '2025-02-28'],
        }
        by_name = {case['name']: case for case in CASES}
        for name, first in expected.items():
            self.assertEqual(by_name[name]['expect'][:len(first)], first, name)


class OracleTests(SimpleTestCase):
    """Where no month is too short for the day, dateutil's rrule gives the same dates."""
    FREQ = {'daily': rrule.DAILY, 'weekly': rrule.WEEKLY, 'monthly': rrule.MONTHLY, 'yearly': rrule.YEARLY}

    def oracle(self, rule, since, count):
        options = {'interval': rule.interval, 'dtstart': rule.start_on, 'wkst': rule.week_start}
        weekdays = [day for day in range(7) if rule.weekday_mask & (1 << day)]
        if rule.freq == 'weekly':
            options['byweekday'] = weekdays or [rule.start_on.weekday()]
        elif rule.set_pos is not None:
            options.update(byweekday=weekdays, bysetpos=rule.set_pos)
        elif rule.month_day is not None:
            options['bymonthday'] = rule.month_day
        if rule.freq == 'yearly' and (rule.set_pos is not None or rule.month_day is not None):
            options['bymonth'] = rule.start_on.month
        found = []
        for moment in rrule.rrule(self.FREQ[rule.freq], **options):
            if moment.date() >= since:
                found.append(moment.date())
            if len(found) == count:
                break
        return found

    def test_against_dateutil(self):
        rules = [
            a_schedule(freq='daily', interval=interval, start_on='2026-01-30') for interval in (1, 2, 7, 45)
        ] + [
            a_schedule(freq='weekly', interval=interval, weekday_mask=mask, week_start=week_start, start_on=start)
            for interval in (1, 2, 3) for mask in (0, 0b0100010, 0b1111111, 0b0010101) for week_start in (0, 5, 6)
            for start in ('2026-10-13', '2026-12-31', '2028-02-29')
        ] + [
            a_schedule(freq='monthly', interval=interval, month_day=day, start_on='2026-10-13')
            for interval in (1, 2, 3, 12) for day in (None, 1, 13, 28, -1)
        ] + [
            a_schedule(freq=freq, interval=interval, weekday_mask=mask, set_pos=position, start_on='2026-10-01')
            for freq in ('monthly', 'yearly') for interval in (1, 2) for mask in (0b0010000, 0b0011111, 0b1100000)
            for position in schedule.SET_POSITIONS
        ] + [
            a_schedule(freq='yearly', interval=interval, month_day=day, start_on='2026-03-26')
            for interval in (1, 4) for day in (None, 26, -1)
        ]
        for rule in rules:
            for since in (rule.start_on, date(2027, 2, 17), date(2031, 12, 31)):
                with self.subTest(vars(rule), since=since):
                    self.assertEqual(schedule.occurrences(rule, since, 12), self.oracle(rule, since, 12))


class EdgesTests(SimpleTestCase):
    def test_nothing_before_the_start_or_after_the_end(self):
        rule = a_schedule(freq='daily', start_on='2026-10-13', end_on='2026-10-15')
        self.assertEqual(days(schedule.occurrences(rule, date(2020, 1, 1), 9)), ['2026-10-13', '2026-10-14', '2026-10-15'])
        self.assertIsNone(schedule.next_on_or_after(rule, date(2026, 10, 16)))

    def test_a_schedule_that_makes_no_sense_is_refused(self):
        bad = [
            {'freq': 'hourly'}, {'interval': 0}, {'interval': 1000}, {'weekday_mask': 128}, {'week_start': 7},
            {'month_day': 0}, {'month_day': 32}, {'month_day': -2},
            {'set_pos': 5, 'weekday_mask': 1}, {'set_pos': 0, 'weekday_mask': 1},
            {'set_pos': 1},                                   # which weekday?
            {'set_pos': 1, 'weekday_mask': 1, 'freq': 'weekly'},
            {'set_pos': 1, 'weekday_mask': 1, 'month_day': 3},
            {'end_on': '2026-10-12'},
        ]
        for fields in bad:
            with self.subTest(fields), self.assertRaises(ValidationError):
                schedule.check(a_schedule(**fields))
