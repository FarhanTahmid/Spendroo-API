from rest_framework.exceptions import ValidationError


def refuse(code, message):
    """A 400 the app can tell apart by its `code`."""
    raise ValidationError({'code': code, 'detail': message})
