from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from attachments import service, storage
from attachments.views import AttachmentRefused, AttachmentUploads
from billing import gates
from billing.catalog.keys import F
from workspaces.tenancy import TenantScopedMixin

from . import receipts
from .models import Transaction, TransactionReceipt
from .receipts import RECEIPT
from .serializers import ReceiptLinkSerializer, TransactionReceiptSerializer


class TransactionReceiptsBase(TenantScopedMixin, APIView):
    """
    Receipts of one personal transaction. The ledger itself changes through
    sync; its files cannot, so they come and go here. Only those who run the
    workspace see the transaction at all: anyone else gets 404.
    """
    permission_classes = [IsAuthenticated]
    billing_gate = gates.GATED

    def get_transaction(self, request, transaction_id):
        transaction = Transaction.objects.select_related('workspace').filter(id=transaction_id).first()
        if transaction is None or not receipts.is_manager(request.user, transaction.workspace):
            raise NotFound('Transaction not found.')
        return transaction

    def get_receipt(self, transaction, receipt_id):
        return get_object_or_404(TransactionReceipt, id=receipt_id, transaction=transaction)


class TransactionReceiptsView(AttachmentUploads, TransactionReceiptsBase):
    """
    POST `receipt_file` (one or more). A phone sends `receipt_id`, an id it
    made, with a single file: sending it again after a lost answer gives back
    the receipt stored the first time (200).
    """
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request={'multipart/form-data': inline_serializer('ReceiptUpload', {
            'receipt_file': serializers.FileField(),
            'receipt_id': serializers.UUIDField(required=False),
        })},
        responses={
            201: inline_serializer('ReceiptsStored', {'receipts': TransactionReceiptSerializer(many=True)}),
            200: inline_serializer('ReceiptsStoredBefore', {'receipts': TransactionReceiptSerializer(many=True)}),
        },
    )
    def post(self, request, transaction_id):
        transaction = self.get_transaction(request, transaction_id)
        try:
            receipts.check_takes_receipts(transaction)
        except receipts.ReceiptNotAllowed:
            raise AttachmentRefused('receipt_not_allowed', 'Only expenses and income take a receipt.')
        # Nothing new goes onto an account a downgrade locked; taking a receipt off always works
        gates.assert_workspace_writable(transaction.workspace, request.user)
        gates.assert_row_writable(transaction.workspace, F.PERSONAL_ACCOUNTS, transaction.account_id)

        results = self.attach(RECEIPT, transaction, self.read_uploads(request, 'receipt_file', 'receipt_id'))
        created = any(fresh for _, fresh in results)
        return Response(
            {'receipts': TransactionReceiptSerializer([receipt for receipt, _ in results], many=True).data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class TransactionReceiptView(TransactionReceiptsBase):
    """DELETE: the receipt goes, and the owner gets the storage back at once."""

    @extend_schema(responses={204: None})
    def delete(self, request, transaction_id, receipt_id):
        receipt = self.get_receipt(self.get_transaction(request, transaction_id), receipt_id)
        service.remove(RECEIPT, receipt)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TransactionReceiptLinkView(TransactionReceiptsBase):
    """GET: a link to the receipt's file, good for a few minutes and only for the one who asked."""

    @extend_schema(responses=ReceiptLinkSerializer)
    def get(self, request, transaction_id, receipt_id):
        receipt = self.get_receipt(self.get_transaction(request, transaction_id), receipt_id)
        url, expires_at = storage.link_for(RECEIPT, receipt, request.user)
        return Response({'url': url, 'expires_at': expires_at})
