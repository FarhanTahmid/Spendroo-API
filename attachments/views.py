import os
import uuid

from django.contrib.auth import get_user_model
from django.core import signing
from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.exceptions import APIException, ParseError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from workspaces.tenancy import activate_tenant_context

from . import files, kinds, service
from .storage import read_link

# Room for the form fields around the largest file the server reads
MULTIPART_SLACK = 1024 * 1024


class AttachmentRefused(APIException):
    """A file that is not a whole, harmless photo or PDF. `code` says which way, for the app to word."""
    status_code = 400

    def __init__(self, code, message):
        super().__init__({'code': code, 'detail': message})


class AttachmentIdTaken(APIException):
    status_code = 409
    # Named for bills, the first kind: the app words every kind's the same way
    default_detail = {'code': 'bill_id_taken', 'detail': 'That id belongs to another attachment.'}


def uuid_or_404(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise Http404


class UploadThrottle(ScopedRateThrottle):
    """Only requests that carry files: every photo is decoded and written out again."""
    scope_attr = 'upload_throttle_scope'

    def allow_request(self, request, view):
        if 'multipart/form-data' not in (request.content_type or ''):
            return True
        return super().allow_request(request, view)


class AttachmentUploads:
    """
    For views that take attachments: a file bigger than any plan takes is
    refused before the body is read, uploads are throttled, and the service's
    refusals become answers.
    """
    upload_throttle_scope = 'bill_upload'

    def get_throttles(self):
        return [*super().get_throttles(), UploadThrottle()]

    def initial(self, request, *args, **kwargs):
        length = request.META.get('CONTENT_LENGTH') or '0'
        if length.isdigit() and int(length) > service.HARD_CAP_MB * service.MB + MULTIPART_SLACK:
            raise AttachmentRefused(files.TOO_LARGE, f'A file can be up to {service.HARD_CAP_MB} MB.')
        super().initial(request, *args, **kwargs)

    def read_uploads(self, request, file_field, id_field):
        """
        [(file, id or None)] from the form. A phone sends `id_field`, an id it
        made, with a single file, so a resend after a lost answer is recognised.
        """
        uploads = request.FILES.getlist(file_field)
        if not uploads:
            raise ParseError('No files provided')
        row_id = request.data.get(id_field) or None
        if row_id is not None:
            if len(uploads) != 1:
                raise ParseError(f'Send one file with a {id_field}')
            try:
                row_id = uuid.UUID(str(row_id))
            except ValueError:
                raise ParseError(f'{id_field} is not an id')
        return [(upload, row_id) for upload in uploads]

    def attach(self, kind, parent, uploads):
        """Store the files (service.attach). Call inside a transaction when the parent is saved in the same request."""
        try:
            return service.attach(kind, parent, uploads, actor=self.request.user)
        except files.FileRejected as error:
            raise AttachmentRefused(error.code, error.message)
        except service.AttachmentConflict:
            raise AttachmentIdTaken()


class AttachmentFileView(APIView):
    """
    The file behind a signed link (attachments/storage.py). The link carries
    no session, so everything is checked here again: signed by this server,
    still in date, made for someone who may still see the row, and the
    attachment still there.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'bill_file'

    def get(self, request, token):
        try:
            kind_name, row_id, user_id = read_link(token)
        except signing.BadSignature:  # expired links included
            return Response({'detail': 'This link has expired. Open the file again.'}, status=status.HTTP_403_FORBIDDEN)
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        kind = kinds.get(kind_name)
        if user is None or kind is None:
            return Response({'detail': 'This link has expired. Open the file again.'}, status=status.HTTP_403_FORBIDDEN)

        activate_tenant_context(user)
        row = kind.model.objects.select_related(*kind.related).filter(id=row_id).first()
        if row is None or not kind.can_view(user, row):
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            handle = kind.file_of(row).open('rb')
        except (FileNotFoundError, OSError):
            return Response(status=status.HTTP_404_NOT_FOUND)

        response = FileResponse(
            handle, content_type=row.content_type, as_attachment=True, filename=download_name(kind, row),
        )
        response['X-Content-Type-Options'] = 'nosniff'
        # Opened by a browser anyway, it can run nothing and load nothing
        response['Content-Security-Policy'] = "default-src 'none'; sandbox"
        response['Referrer-Policy'] = 'no-referrer'
        response['Cache-Control'] = 'private, max-age=300'
        return response


def download_name(kind, row):
    """The name the person gave the file, ending in what the file really is."""
    extension = files.EXTENSIONS.get(row.content_type, 'bin')
    stem = os.path.splitext(row.original_name)[0] or kind.name
    return f'{stem}.{extension}'
