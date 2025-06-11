import frappe
from frappe.desk.reportview import (
    get_form_params,
    is_virtual_doctype,
    get_controller,
    compress,
    execute,
)


@frappe.whitelist()
@frappe.read_only()
def get():
    args = get_form_params()

    # If virtual doctype, get data from controller get_list method
    if is_virtual_doctype(args.doctype):
        controller = get_controller(args.doctype)
        data = compress(frappe.call(controller.get_list, args=args, **args))
    else:
        data = compress(execute(**args), args=args)

    keys = data.get("keys", [])
    values = data.get("values", [])

    actual_qty_index = keys.index("custom_actual_qty") if "custom_actual_qty" in keys else -1
    reserved_qty_index = keys.index("custom_reserved_qty") if "custom_reserved_qty" in keys else -1

    items = [i[0] for i in values]

    items.append("my item 1")
    items.append("my item 2")

    bin_data = frappe.db.sql(
        f"select item_code, SUM(reserved_qty) as reserved_qty, SUM(actual_qty) as actual_qty from `tabBin` where item_code in {tuple(items)} group by item_code ",
        as_dict=True,
        debug=True
    )
    item_wise_bin_data = {}
    for bin in bin_data:
        item_wise_bin_data[bin.item_code] = {
            "actual_qty": bin.actual_qty,
            "reserved_qty": bin.reserved_qty,
        }

    for i in data["values"]:
        item_code = i[0]
        if item_code in item_wise_bin_data:
            if actual_qty_index != -1:
                i[actual_qty_index] = item_wise_bin_data[item_code]["actual_qty"]
            if reserved_qty_index != -1:
                i[reserved_qty_index] = item_wise_bin_data[item_code]["reserved_qty"]

    return data
