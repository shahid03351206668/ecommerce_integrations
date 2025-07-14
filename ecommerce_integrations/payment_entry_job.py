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
    """,
        as_dict=True,
    )

    for i in unpaid_invoices:
        try:

            doc = frappe.get_doc("Sales Invoice", i.get("invoice"))
            payment = get_payment_entry(dt="Sales Invoice", dn=doc.name)
            payment.reference_no = doc.name
            payment.paid_amount = doc.outstanding_amount
            payment.save()
            payment.submit()
        except Exception as e:
            frappe.log_error(
                "error while creating payment entry of invoice %s" % i.get("invoice"), str(e)
            )
