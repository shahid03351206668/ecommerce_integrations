import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry


def main():

    unpaid_invoices = frappe.db.sql(
    f""" 
        SELECT 
            DISTINCT si.name as invoice
        FROM `tabSales Order` so 
        INNER JOIN `tabSales Invoice Item` sii  on sii.sales_order = so.name  
        INNER JOIN `tabSales Invoice` si  on si.name = sii.parent  
        WHERE  outstanding_amount > 0
        AND si.docstatus = 1
        AND so.custom_payment_status = 'Paid'
        AND so.transaction_date = '{frappe.utils.getdate()}'
    """,
        as_dict=True,
    )
    paid_to_account = "1151 - Durchlaufskonto ecom Zahlungen - VA"
    use_custom = False
    if frappe.db.exists("Account", paid_to_account):
        use_custom = True
    for i in unpaid_invoices:
        try:

            doc = frappe.get_doc("Sales Invoice", i.get("invoice"))
            posting_date = None
            for i in doc.items:
                if i.sales_order:
                    posting_date = frappe.db.get_value("Sales Order", i.sales_order, "transaction_date")
                    break

            payment = get_payment_entry(dt="Sales Invoice", dn=doc.name)

            if use_custom:
                payment.paid_to = paid_to_account


            payment.posting_date = posting_date
            payment.reference_no = doc.name
            payment.paid_amount = doc.outstanding_amount
            payment.save()
            payment.submit()
        except Exception as e:
            frappe.log_error(
                "error while creating payment entry of invoice %s" % i.get("invoice"), str(e)
            )
