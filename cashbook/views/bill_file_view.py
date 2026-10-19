import os

from django.contrib.auth import get_user_model
from django.core import signing
from django.http import FileResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from workspaces.tenancy import activate_tenant_context

from ..bill_files import EXTENSIONS
from ..bill_storage import read_link
from ..models import EntryBills


class BillFileView(APIView):
    """
    The file behind a signed link (cashbook/bill_storage.py). The link carries
    no session, so everything is checked here again: signed by this server,
    still in date, made for someone who may still see the book, and the bill
    still there.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'bill_file'

    def get(self, request, token):
        try:
            bill_id, user_id = read_link(token)
        except signing.BadSignature:  # expired links included
            return Response({'detail': 'This link has expired. Open the bill again.'}, status=status.HTTP_403_FORBIDDEN)
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        if user is None:
            return Response({'detail': 'This link has expired. Open the bill again.'}, status=status.HTTP_403_FORBIDDEN)

        activate_tenant_context(user)
        bill = EntryBills.objects.select_related('cashbook__workspace').filter(id=bill_id).first()
        if bill is None or not bill.cashbook.has_permission(user, 'view'):
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            handle = bill.bill_file.open('rb')
        except (FileNotFoundError, OSError):
            return Response(status=status.HTTP_404_NOT_FOUND)

        response = FileResponse(
            handle, content_type=bill.content_type, as_attachment=True, filename=download_name(bill),
        )
        response['X-Content-Type-Options'] = 'nosniff'
        # Opened by a browser anyway, it can run nothing and load nothing
        response['Content-Security-Policy'] = "default-src 'none'; sandbox"
        response['Referrer-Policy'] = 'no-referrer'
        response['Cache-Control'] = 'private, max-age=300'
        return response


def download_name(bill):
    """The name the person gave the file, ending in what the file really is."""
    extension = EXTENSIONS.get(bill.content_type, 'bin')
    stem = os.path.splitext(bill.original_name)[0] or 'bill'
    return f'{stem}.{extension}'
