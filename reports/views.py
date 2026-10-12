"""
Reports to download: GET /api/v1/reports/<what>/?file_type&period&from&to&lang

A PDF of a preset period is for every plan. Excel, and a custom report (chosen
dates, or narrowed with &type and &category), are plan flags
(billing/catalog/keys.py), judged on the plan of whoever owns the workspace.
Each download is noted in ReportDownload.
"""
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import APIException, NotFound, ParseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from billing import gates
from billing.catalog.keys import F
from cashbook.models import CashBook, EntryCategory
from ledger_personal.models import Account, Category
from workspaces.models import Membership, Workspace
from workspaces.tenancy import TenantScopedMixin

from . import narrow as narrowing
from . import periods
from .builders import account, business, cashbook, personal
from .builders.common import Context
from .labels import DEFAULT_LANGUAGE, LANGUAGES, Words
from .models import (
    FILE_PDF, FILE_XLSX, SCOPE_ACCOUNT, SCOPE_BUSINESS, SCOPE_CASHBOOK, SCOPE_PERSONAL, ReportDownload, new_reference,
)
from .render import pdf, xlsx

RENDERERS = {
    FILE_PDF: (pdf.render, 'application/pdf'),
    FILE_XLSX: (xlsx.render, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}
# Detail rows a file may hold. A PDF is laid out page by page while the request waits.
MAX_ROWS = {FILE_PDF: 2000, FILE_XLSX: 20000}
# Those who see every cashbook of a business
WHOLE_BUSINESS_ROLES = (Membership.ROLE_OWNER, Membership.ROLE_ADMIN, Membership.ROLE_VIEWER)

PARAMETERS = [
    OpenApiParameter('file_type', OpenApiTypes.STR, enum=list(RENDERERS), description='pdf by default.'),
    OpenApiParameter('period', OpenApiTypes.STR, enum=list(periods.ALL), description='this_month by default.'),
    OpenApiParameter('from', OpenApiTypes.DATE, description='First day, for period=custom.'),
    OpenApiParameter('to', OpenApiTypes.DATE, description='Last day, for period=custom.'),
    OpenApiParameter(
        'type', OpenApiTypes.STR, many=True,
        description='Custom: only these kinds of line. income, expense, transfer for personal reports; '
                    'cash_in, cash_out for cashbooks and businesses.',
    ),
    OpenApiParameter(
        'category', OpenApiTypes.STR, many=True,
        description="Custom: only these category ids; 'none' for lines with no category.",
    ),
    OpenApiParameter('lang', OpenApiTypes.STR, enum=list(LANGUAGES), description='Language of the headings.'),
]


class ReportTooLarge(APIException):
    status_code = 400
    default_code = 'report_too_large'

    def __init__(self, rows, limit):
        self.detail = {
            'code': self.default_code,
            'detail': f'This period has {rows} lines; a report holds up to {limit}. Choose a shorter period.',
            'rows': rows,
            'limit': limit,
        }


def _reference():
    while True:
        reference = new_reference()
        if not ReportDownload.objects.filter(reference=reference).exists():
            return reference


class ReportView(TenantScopedMixin, APIView):
    """One kind of report. A subclass finds what it is about and builds it."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'reports'
    scope = None
    #: The kinds of line ?type may name
    types = narrowing.LEDGER_TYPES

    def categories(self, workspace, subject):
        """(the categories ?category may name, the field holding their name)."""
        raise NotImplementedError

    def find(self, request, **kwargs):
        """(workspace, the thing reported on, its name). Raises NotFound for anyone who may not see it."""
        raise NotImplementedError

    def build(self, subject, context):
        raise NotImplementedError

    @extend_schema(parameters=PARAMETERS, responses={(200, 'application/pdf'): OpenApiTypes.BINARY})
    def get(self, request, **kwargs):
        params = request.query_params
        file_type = params.get('file_type') or FILE_PDF
        if file_type not in RENDERERS:
            raise ParseError(f"file_type must be one of: {', '.join(RENDERERS)}.")
        language = params.get('lang') or DEFAULT_LANGUAGE
        if language not in LANGUAGES:
            raise ParseError(f"lang must be one of: {', '.join(LANGUAGES)}.")
        period = periods.resolve(params, timezone.localdate())
        narrow = narrowing.parse(params, self.types)

        # Who may see it comes first, so a stranger learns nothing about the owner's plan
        workspace, subject, name = self.find(request, **kwargs)
        if file_type == FILE_XLSX:
            gates.require(workspace, F.REPORTS_EXCEL)
        if period.is_custom or narrow.active:
            gates.require(workspace, F.REPORTS_CUSTOM_RANGE)
        narrow = narrowing.named(narrow, *self.categories(workspace, subject))

        context = Context(
            user=request.user, period=period, words=Words(language),
            reference=_reference(), generated_at=timezone.now(), narrow=narrow,
        )
        report = self.build(subject, context)
        if report.row_count > MAX_ROWS[file_type]:
            raise ReportTooLarge(report.row_count, MAX_ROWS[file_type])
        render, content_type = RENDERERS[file_type]
        content = render(report)

        ReportDownload.objects.create(
            user=request.user, workspace=workspace, scope=self.scope, subject_id=subject.id, subject_name=name[:100],
            file_type=file_type, period=period.name, date_from=period.date_from, date_to=period.date_to,
            language=language, row_count=report.row_count, reference=context.reference,
            filters={label: chosen for label, chosen in narrowing.describe(narrow, Words())},
        )
        # Names may be Bangla; the file name keeps to what every phone can save
        filename = '-'.join(filter(None, [
            'spendroo', self.scope, slugify(name)[:40], f'{period.date_from:%Y%m%d}', f'{period.date_to:%Y%m%d}',
        ]))
        response = HttpResponse(content, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}.{file_type}"'
        response['X-Report-Reference'] = context.reference
        return response


class PersonalReportView(ReportView):
    """Every personal account together: income against spending."""
    scope = SCOPE_PERSONAL

    def find(self, request):
        workspace = Workspace.objects.filter(owner=request.user, kind=Workspace.KIND_PERSONAL).first()
        if workspace is None:
            raise NotFound('Report not found.')
        return workspace, workspace, workspace.name

    def categories(self, workspace, subject):
        return Category.objects.filter(workspace=workspace), 'name'

    def build(self, subject, context):
        return personal.build(subject, context)


class AccountReportView(ReportView):
    """The statement of one personal account."""
    scope = SCOPE_ACCOUNT

    def find(self, request, account_id):
        found = Account.objects.select_related('workspace').filter(id=account_id).first()
        # The personal ledger is for those who run the workspace, as it is in sync
        if found is None or found.workspace.role_of(request.user) not in Membership.MANAGER_ROLES:
            raise NotFound('Report not found.')
        return found.workspace, found, found.name

    def categories(self, workspace, subject):
        return Category.objects.filter(workspace=workspace), 'name'

    def build(self, subject, context):
        return account.build(subject, context)


class CashbookReportView(ReportView):
    """One cashbook, for anyone who may open it."""
    scope = SCOPE_CASHBOOK
    types = narrowing.CASHBOOK_TYPES

    def find(self, request, cashbook_id):
        book = CashBook.objects.select_related('workspace').filter(id=cashbook_id).first()
        if book is None or not book.has_permission(request.user, 'view'):
            raise NotFound('Report not found.')
        return book.workspace, book, book.book_name

    def categories(self, workspace, subject):
        return EntryCategory.objects.filter(cashbook=subject), 'category_name'

    def build(self, subject, context):
        return cashbook.build(subject, context)


class BusinessReportView(ReportView):
    """Every cashbook of a business, for those who see them all."""
    scope = SCOPE_BUSINESS
    types = narrowing.CASHBOOK_TYPES

    def find(self, request, workspace_id):
        workspace = Workspace.objects.filter(id=workspace_id, kind=Workspace.KIND_BUSINESS).first()
        if workspace is None or workspace.role_of(request.user) not in WHOLE_BUSINESS_ROLES:
            raise NotFound('Report not found.')
        return workspace, workspace, workspace.name

    def categories(self, workspace, subject):
        return EntryCategory.objects.filter(workspace=workspace), 'category_name'

    def build(self, subject, context):
        return business.build(subject, context)
