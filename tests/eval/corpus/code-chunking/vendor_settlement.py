"""Vendor settlement reconciliation batch — heavy vocabulary overlap with
the refund query (settlement, reconciliation, ledger, batch, quorum,
chargeback) but is about vendor invoices, not customer refunds."""


def reconcile_vendor_settlement_batch(vendor_id, vendor_ledger, quorum_size):
    """Reconcile a vendor settlement batch against the vendor ledger.

    Settlement reconciliation batch quorum chargeback ledger.
    """
    total = sum(vendor_ledger.values())
    return {
        "vendor_id": vendor_id,
        "reconciliation_total": total,
        "quorum_size": quorum_size,
        "chargeback_flag": total < 0,
    }
