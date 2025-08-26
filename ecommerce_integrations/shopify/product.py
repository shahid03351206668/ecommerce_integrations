from typing import Optional

import frappe
import shopify
from shopify.resources import Product, Variant
from frappe import _, msgprint
from frappe.utils import cint, cstr
from frappe.utils.nestedset import get_root_of

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import (
    ecommerce_item,
)
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
    ITEM_SELLING_RATE_FIELD,
    MODULE_NAME,
    SETTING_DOCTYPE,
    SHOPIFY_VARIANTS_ATTR_LIST,
    SUPPLIER_ID_FIELD,
    WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopify.utils import create_shopify_log


class ShopifyProduct:
    def __init__(
        self,
        product_id: str,
        variant_id: Optional[str] = None,
        sku: Optional[str] = None,
        has_variants: Optional[int] = 0,
    ):
        self.product_id = str(product_id)
        self.variant_id = str(variant_id) if variant_id else None
        self.sku = str(sku) if sku else None
        self.has_variants = has_variants
        self.setting = frappe.get_doc(SETTING_DOCTYPE)

        if not self.setting.is_enabled():
            frappe.throw(
                _("Can not create Shopify product when integration is disabled.")
            )

    def is_synced(self) -> bool:
        return ecommerce_item.is_synced(
            MODULE_NAME,
            integration_item_code=self.product_id,
            variant_id=self.variant_id,
            sku=self.sku,
        )

    def get_erpnext_item(self):
        return ecommerce_item.get_erpnext_item(
            MODULE_NAME,
            integration_item_code=self.product_id,
            variant_id=self.variant_id,
            sku=self.sku,
            has_variants=self.has_variants,
        )

    @temp_shopify_session
    def sync_product(self):
        if not self.is_synced():
            shopify_product = Product.find(self.product_id)
            product_dict = shopify_product.to_dict()
            self._make_item(product_dict)

    def _make_item(self, product_dict):
        _add_weight_details(product_dict)

        warehouse = self.setting.warehouse

        if _has_variants(product_dict):
            self.has_variants = 1
            attributes = self._create_attribute(product_dict)
            self._create_item(product_dict, warehouse, 1, attributes)
            self._create_item_variants(product_dict, warehouse, attributes)

        else:
            product_dict["variant_id"] = product_dict["variants"][0]["id"]
            self._create_item(product_dict, warehouse)

    def _create_attribute(self, product_dict):
        attribute = []
        for attr in product_dict.get("options"):
            if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
                frappe.get_doc(
                    {
                        "doctype": "Item Attribute",
                        "attribute_name": attr.get("name"),
                        "item_attribute_values": [
                            {"attribute_value": attr_value, "abbr": attr_value}
                            for attr_value in attr.get("values")
                        ],
                    }
                ).insert()
                attribute.append({"attribute": attr.get("name")})

            else:
                # check for attribute values
                item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
                if not item_attr.numeric_values:
                    self._set_new_attribute_values(item_attr, attr.get("values"))
                    item_attr.save()
                    attribute.append({"attribute": attr.get("name")})

                else:
                    attribute.append(
                        {
                            "attribute": attr.get("name"),
                            "from_range": item_attr.get("from_range"),
                            "to_range": item_attr.get("to_range"),
                            "increment": item_attr.get("increment"),
                            "numeric_values": item_attr.get("numeric_values"),
                        }
                    )

        return attribute

    def _set_new_attribute_values(self, item_attr, values):
        for attr_value in values:
            if not any(
                (
                    d.abbr.lower() == attr_value.lower()
                    or d.attribute_value.lower() == attr_value.lower()
                )
                for d in item_attr.item_attribute_values
            ):
                item_attr.append(
                    "item_attribute_values",
                    {"attribute_value": attr_value, "abbr": attr_value},
                )

    def _create_item(
        self, product_dict, warehouse, has_variant=0, attributes=None, variant_of=None
    ):
        item_dict = {
            "variant_of": variant_of,
            "is_stock_item": 1,
            "item_code": cstr(product_dict.get("item_code"))
            or cstr(product_dict.get("id")),
            "item_name": product_dict.get("title", "").strip(),
            "description": product_dict.get("body_html") or product_dict.get("title"),
            "item_group": self._get_item_group(product_dict.get("product_type")),
            "has_variants": has_variant,
            "attributes": attributes or [],
            "stock_uom": product_dict.get("uom") or _("Nos"),
            "sku": product_dict.get("sku") or _get_sku(product_dict),
            "default_warehouse": warehouse,
            "image": _get_item_image(product_dict),
            "weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP[product_dict.get("weight_unit")],
            "weight_per_unit": product_dict.get("weight"),
            "default_supplier": self._get_supplier(product_dict),
        }

        integration_item_code = product_dict["id"]  # shopify product_id
        variant_id = product_dict.get(
            "variant_id", ""
        )  # shopify variant_id if has variants
        sku = item_dict["sku"]

        if not _match_sku_and_link_item(
            item_dict,
            integration_item_code,
            variant_id,
            variant_of=variant_of,
            has_variant=has_variant,
        ):
            ecommerce_item.create_ecommerce_item(
                MODULE_NAME,
                integration_item_code,
                item_dict,
                variant_id=variant_id,
                sku=sku,
                variant_of=variant_of,
                has_variants=has_variant,
            )

    def _create_item_variants(self, product_dict, warehouse, attributes):
        template_item = ecommerce_item.get_erpnext_item(
            MODULE_NAME, integration_item_code=product_dict.get("id"), has_variants=1
        )

        if template_item:
            for variant in product_dict.get("variants"):
                shopify_item_variant = {
                    "id": product_dict.get("id"),
                    "variant_id": variant.get("id"),
                    "item_code": variant.get("id"),
                    "title": product_dict.get("title", "").strip()
                    + "-"
                    + variant.get("title"),
                    "product_type": product_dict.get("product_type"),
                    "sku": variant.get("sku"),
                    "uom": template_item.stock_uom or _("Nos"),
                    "item_price": variant.get("price"),
                    "weight_unit": variant.get("weight_unit"),
                    "weight": variant.get("weight"),
                }

                for i, variant_attr in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
                    if variant.get(variant_attr):
                        attributes[i].update(
                            {
                                "attribute_value": self._get_attribute_value(
                                    variant.get(variant_attr), attributes[i]
                                )
                            }
                        )
                self._create_item(
                    shopify_item_variant, warehouse, 0, attributes, template_item.name
                )

    def _get_attribute_value(self, variant_attr_val, attribute):
        attribute_value = frappe.db.sql(
            """select attribute_value from `tabItem Attribute Value`
			where parent = %s and (abbr = %s or attribute_value = %s)""",
            (attribute["attribute"], variant_attr_val, variant_attr_val),
            as_list=1,
        )
        return (
            attribute_value[0][0]
            if len(attribute_value) > 0
            else cint(variant_attr_val)
        )

    def _get_item_group(self, product_type=None):
        parent_item_group = get_root_of("Item Group")

        if not product_type:
            return parent_item_group

        if frappe.db.get_value("Item Group", product_type, "name"):
            return product_type
        item_group = frappe.get_doc(
            {
                "doctype": "Item Group",
                "item_group_name": product_type,
                "parent_item_group": parent_item_group,
                "is_group": "No",
            }
        ).insert()
        return item_group.name

    def _get_supplier(self, product_dict):
        if product_dict.get("vendor"):
            supplier = frappe.db.sql(
                f"""select name from tabSupplier
				where name = %s or {SUPPLIER_ID_FIELD} = %s """,
                (product_dict.get("vendor"), product_dict.get("vendor").lower()),
                as_list=1,
            )

            if supplier:
                return product_dict.get("vendor")

            supplier = frappe.new_doc("Supplier")
            supplier.supplier_name = product_dict.get("vendor")
            supplier.supplier_group = self._get_supplier_group()
            setattr(supplier, SUPPLIER_ID_FIELD, product_dict.get("vendor").lower())

            supplier.flags.ignore_permissions = True
            supplier.flags.ignore_mandatory = True
            supplier.save()

            # supplier = frappe.get_doc(
            # 	{
            # 		"doctype": "Supplier",
            # 		"supplier_name": product_dict.get("vendor"),
            # 		SUPPLIER_ID_FIELD: product_dict.get("vendor").lower(),
            # 		"supplier_group": self._get_supplier_group(),
            # 	}
            # ).insert()
            return supplier.name
        else:
            return ""

    def _get_supplier_group(self):
        supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
        if not supplier_group:
            supplier_group = frappe.get_doc(
                {
                    "doctype": "Supplier Group",
                    "supplier_group_name": _("Shopify Supplier"),
                }
            ).insert()
            return supplier_group.name
        return supplier_group


def _add_weight_details(product_dict):
    variants = product_dict.get("variants")
    if variants:
        product_dict["weight"] = variants[0]["weight"]
        product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
    options = product_dict.get("options")
    return bool(options and "Default Title" not in options[0]["values"])


def _get_sku(product_dict):
    if product_dict.get("variants"):
        return product_dict.get("variants")[0].get("sku")
    return ""


def _get_item_image(product_dict):
    if product_dict.get("image"):
        return product_dict.get("image").get("src")
    return None


def _match_sku_and_link_item(
    item_dict, product_id, variant_id, variant_of=None, has_variant=False
) -> bool:
    """Tries to match new item with existing item using Shopify SKU == item_code.

    Returns true if matched and linked.
    """
    sku = item_dict["sku"]
    if not sku or variant_of or has_variant:
        return False

    item_name = frappe.db.get_value("Item", {"item_code": sku})
    if item_name:
        try:
            ecommerce_item = frappe.get_doc(
                {
                    "doctype": "Ecommerce Item",
                    "integration": MODULE_NAME,
                    "erpnext_item_code": item_name,
                    "integration_item_code": product_id,
                    "has_variants": 0,
                    "variant_id": cstr(variant_id),
                    "sku": sku,
                }
            )

            ecommerce_item.insert()
            return True
        except Exception:
            return False


def create_items_if_not_exist(order):
    """Using shopify order, sync all items that are not already synced."""
    for item in order.get("line_items", []):
        product_id = item["product_id"]
        variant_id = item.get("variant_id")
        sku = item.get("sku")
        product = ShopifyProduct(product_id, variant_id=variant_id, sku=sku)

        if not product.is_synced():
            product.sync_product()


def get_item_code(shopify_item):
    """Get item code using shopify_item dict.

    Item should contain both product_id and variant_id."""

    item = ecommerce_item.get_erpnext_item(
        integration=MODULE_NAME,
        integration_item_code=shopify_item.get("product_id"),
        variant_id=shopify_item.get("variant_id"),
        sku=shopify_item.get("sku"),
    )
    if item:
        return item.item_code


@temp_shopify_session
def upload_erpnext_item(doc, method=None):
    """This hook is called when inserting new or updating existing `Item`.

    New items are pushed to shopify and changes to existing items are
    updated depending on what is configured in "Shopify Setting" doctype.
    """
    template_item = item = doc  # alias for readability
    # a new item recieved from ecommerce_integrations is being inserted
    if item.flags.from_integration:
        return

    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled() or not setting.upload_erpnext_items:
        return

    if frappe.flags.in_import:
        return

    if item.has_variants:
        return

    if len(item.attributes) > 3:
        msgprint(
            _(
                "Template items/Items with 4 or more attributes can not be uploaded to Shopify."
            )
        )
        return

    if doc.variant_of and not setting.upload_variants_as_items:
        msgprint(_("Enable variant sync in setting to upload item to Shopify."))
        return

    if item.variant_of:
        template_item = frappe.get_doc("Item", item.variant_of)

    product_id = frappe.db.get_value(
        "Ecommerce Item",
        {"erpnext_item_code": template_item.name, "integration": MODULE_NAME},
        "integration_item_code",
    )
    is_new_product = not bool(product_id)

    if is_new_product:
        product = Product()
        product.published = False
        product.status = "active" if setting.sync_new_item_as_active else "draft"

        map_erpnext_item_to_shopify(shopify_product=product, erpnext_item=template_item)
        is_successful = product.save()

        if is_successful:
            update_default_variant_properties(
                product,
                sku=template_item.item_code,
                price=template_item.get(ITEM_SELLING_RATE_FIELD),
                is_stock_item=template_item.is_stock_item,
            )
            if item.variant_of:
                product.options = []
                product.variants = []
                variant_attributes = {
                    "title": template_item.item_name,
                    "sku": item.item_code,
                    "price": item.get(ITEM_SELLING_RATE_FIELD),
                }
                max_index_range = min(3, len(template_item.attributes))
                for i in range(0, max_index_range):
                    attr = template_item.attributes[i]
                    product.options.append(
                        {
                            "name": attr.attribute,
                            "values": frappe.db.get_all(
                                "Item Attribute Value",
                                {"parent": attr.attribute},
                                pluck="attribute_value",
                            ),
                        }
                    )
                    try:
                        variant_attributes[f"option{i + 1}"] = item.attributes[
                            i
                        ].attribute_value
                    except IndexError:
                        frappe.throw(
                            _("Shopify Error: Missing value for attribute {}").format(
                                attr.attribute
                            )
                        )
                product.variants.append(Variant(variant_attributes))

            product.save()  # push variant

            ecom_items = list(set([item, template_item]))
            for d in ecom_items:
                ecom_item = frappe.get_doc(
                    {
                        "doctype": "Ecommerce Item",
                        "erpnext_item_code": d.name,
                        "integration": MODULE_NAME,
                        "integration_item_code": str(product.id),
                        "variant_id": ""
                        if d.has_variants
                        else str(product.variants[0].id),
                        "sku": "" if d.has_variants else str(product.variants[0].sku),
                        "has_variants": d.has_variants,
                        "variant_of": d.variant_of,
                    }
                )
                ecom_item.insert()

        write_upload_log(status=is_successful, product=product, item=item)
    elif setting.update_shopify_item_on_update:
        product = Product.find(product_id)
        if product:
            map_erpnext_item_to_shopify(
                shopify_product=product, erpnext_item=template_item
            )
            if not item.variant_of:
                update_default_variant_properties(
                    product,
                    is_stock_item=template_item.is_stock_item,
                    price=item.get(ITEM_SELLING_RATE_FIELD),
                )
            else:
                variant_attributes = {
                    "sku": item.item_code,
                    "price": item.get(ITEM_SELLING_RATE_FIELD),
                }
                product.options = []
                max_index_range = min(3, len(template_item.attributes))
                for i in range(0, max_index_range):
                    attr = template_item.attributes[i]
                    product.options.append(
                        {
                            "name": attr.attribute,
                            "values": frappe.db.get_all(
                                "Item Attribute Value",
                                {"parent": attr.attribute},
                                pluck="attribute_value",
                            ),
                        }
                    )
                    try:
                        variant_attributes[f"option{i + 1}"] = item.attributes[
                            i
                        ].attribute_value
                    except IndexError:
                        frappe.throw(
                            _("Shopify Error: Missing value for attribute {}").format(
                                attr.attribute
                            )
                        )
                product.variants.append(Variant(variant_attributes))

            is_successful = product.save()
            if is_successful and item.variant_of:
                map_erpnext_variant_to_shopify_variant(
                    product, item, variant_attributes
                )

            write_upload_log(
                status=is_successful, product=product, item=item, action="Updated"
            )

def map_erpnext_variant_to_shopify_variant(
    shopify_product: Product, erpnext_item, variant_attributes
):
    variant_product_id = frappe.db.get_value(
        "Ecommerce Item",
        {"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
        "integration_item_code",
    )
    if not variant_product_id:
        for variant in shopify_product.variants:
            if (
                variant.option1 == variant_attributes.get("option1")
                and variant.option2 == variant_attributes.get("option2")
                and variant.option3 == variant_attributes.get("option3")
            ):
                variant_product_id = str(variant.id)
                if not frappe.flags.in_test:
                    frappe.get_doc(
                        {
                            "doctype": "Ecommerce Item",
                            "erpnext_item_code": erpnext_item.name,
                            "integration": MODULE_NAME,
                            "integration_item_code": str(shopify_product.id),
                            "variant_id": variant_product_id,
                            "sku": str(variant.sku),
                            "variant_of": erpnext_item.variant_of,
                        }
                    ).insert()
                break
        if not variant_product_id:
            msgprint(_("Shopify: Couldn't sync item variant."))
    return variant_product_id


import re
import frappe
from frappe import _
from frappe.utils import cstr

def map_product_meta_fields(shopify_product, erpnext_item):
    """Map ERPNext custom fields to Shopify metafields with improved error handling."""
    
    meta_fields_maps = [
        {
            "namespace": "custom",
            "key": "zubereitung_oder_anwendung",
            "type": "multi_line_text_field",
            "erpnext_field": "custom_zubereitung_oder_anwendung",
        },
        {
            "namespace": "custom",
            "key": "zutaten",
            "type": "multi_line_text_field",
            "erpnext_field": "custom_zutaten",
        },
        {
            "namespace": "custom",
            "key": "anwendung_zubereitung",
            "type": "multi_line_text_field",
            "erpnext_field": "custom_zubereitunganwendung",
        },
        {
            "namespace": "custom",
            "key": "salz",
            "type": "number_decimal",
            "erpnext_field": "custom_salt",
        },
        {
            "namespace": "custom",
            "key": "eiweiss",
            "type": "number_decimal",
            "erpnext_field": "custom_protein",
        },
        {
            "namespace": "custom",
            "key": "davon_zucker",
            "type": "number_decimal",
            "erpnext_field": "custom_of_which_sugar",
        },
        {
            "namespace": "custom",
            "key": "kohlenhydrate",
            "type": "number_decimal",
            "erpnext_field": "custom_carbohydrates",
        },
        {
            "namespace": "custom",
            "key": "gesattigte_fettsauren",
            "type": "number_decimal",
            "erpnext_field": "custom_saturated_fats",
        },
        {
            "namespace": "custom",
            "key": "fett",
            "type": "number_decimal",
            "erpnext_field": "custom_fat",
        },
        {
            "namespace": "custom",
            "key": "kj",
            "type": "number_integer",
            "erpnext_field": "custom_kj",
        },
        {
            "namespace": "custom",
            "key": "kcal",
            "type": "number_integer",
            "erpnext_field": "custom_kcal",
        },
        {
            "namespace": "custom",
            "key": "ohne_palmol",
            "type": "boolean",
            "erpnext_field": "custom_without_palm_oil",
        },
        {
            "namespace": "custom",
            "key": "glutenfrei",
            "type": "boolean",
            "erpnext_field": "custom_glutenfree",
        },
        {
            "namespace": "custom",
            "key": "glutamatfrei_msg",
            "type": "boolean",
            "erpnext_field": "custom_glutamatefree_msg",
        },
        {
            "namespace": "custom",
            "key": "laktosefrei",
            "type": "boolean",
            "erpnext_field": "custom_lactosefree",
        },
    ]
    
    meta_fields_added = []
    meta_fields_errors = []

    for field_map in meta_fields_maps:
        erpnext_field = field_map.get("erpnext_field")
        field_type = field_map.get("type")
        shopify_key = field_map.get("key")
        
        # Get value from ERPNext item
        raw_value = erpnext_item.get(erpnext_field)
        
        # Skip if no value and not boolean type (booleans can be False)
        if raw_value is None and field_type != "boolean":
            continue
            
        # Process the value based on field type
        processed_value = process_metafield_value(raw_value, field_type, erpnext_field)
        
        # Skip if processed value is empty/invalid
        if processed_value is None:
            continue
            
        try:
            # Create metafield object
            metafield_data = {
                "namespace": field_map.get("namespace"),
                "key": shopify_key,
                "type": field_type,
                "value": processed_value,
            }
            
            # Add metafield to product
            shopify_product.add_metafield(shopify.Metafield(metafield_data))
            
            meta_fields_added.append({
                "key": erpnext_field,
                "shopify_key": shopify_key,
                "value": processed_value,
                "type": field_type
            })
            
        except Exception as e:
            error_msg = f"Failed to add metafield {shopify_key}: {str(e)}"
            meta_fields_errors.append({
                "field": erpnext_field,
                "shopify_key": shopify_key,
                "error": str(e)
            })
            frappe.log_error(
                f"Metafield Error - {shopify_key}",
                f"{error_msg}\n\nField Map: {field_map}\nValue: {processed_value}\n\n{frappe.get_traceback()}"
            )

    # Log results
    if meta_fields_added:
        frappe.log_error("Metafields Successfully Added", frappe.as_json(meta_fields_added, indent=2))
    
    if meta_fields_errors:
        frappe.log_error("Metafield Errors Summary", frappe.as_json(meta_fields_errors, indent=2))


def process_metafield_value(raw_value, field_type, erpnext_field):
    """Process and validate metafield values based on their type."""
    
    if raw_value is None:
        return None if field_type != "boolean" else False
    
    # Handle HTML content extraction for specific fields
    if erpnext_field in ["custom_zutaten", "custom_zubereitunganwendung"]:
        if cstr(raw_value):
            # Extract content from HTML paragraph tags
            match = re.search(r"<p>(.*?)</p>", cstr(raw_value), re.DOTALL | re.IGNORECASE)
            if match:
                # Clean up the extracted content
                extracted = match.group(1).strip()
                # Remove any remaining HTML tags
                clean_value = re.sub(r'<[^>]+>', '', extracted).strip()
                return clean_value if clean_value else None
            else:
                # If no <p> tags, remove all HTML tags
                clean_value = re.sub(r'<[^>]+>', '', cstr(raw_value)).strip()
                return clean_value if clean_value else None
        return None
    
    # Handle different field types
    if field_type == "boolean":
        # Convert to boolean, handling various truthy/falsy values
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        if isinstance(raw_value, str):
            return raw_value.lower() in ['true', '1', 'yes', 'on']
        return bool(raw_value)
    
    elif field_type in ["number_decimal", "number_integer"]:
        try:
            if isinstance(raw_value, (int, float)):
                return int(raw_value) if field_type == "number_integer" else float(raw_value)
            
            # Handle string numbers
            value_str = cstr(raw_value).strip()
            if not value_str:
                return None
                
            # Remove any non-numeric characters except decimal point and minus
            cleaned = re.sub(r'[^\d.-]', '', value_str)
            if not cleaned or cleaned in ['-', '.', '-.']:
                return None
                
            return int(float(cleaned)) if field_type == "number_integer" else float(cleaned)
            
        except (ValueError, TypeError):
            frappe.log_error(
                f"Invalid numeric value for {erpnext_field}",
                f"Raw value: {raw_value}, Type: {type(raw_value)}"
            )
            return None
    
    elif field_type in ["single_line_text_field", "multi_line_text_field"]:
        text_value = cstr(raw_value).strip()
        return text_value if text_value else None
    
    # Default case
    return cstr(raw_value).strip() if cstr(raw_value).strip() else None


def map_erpnext_item_to_shopify(shopify_product, erpnext_item):
    """Map ERPNext fields to Shopify product with improved error handling and validation."""
    
    if not shopify_product or not erpnext_item:
        frappe.throw(_("Invalid product or item data provided"))
    
    try:
        # Map basic product information
        if hasattr(erpnext_item, 'item_name') and erpnext_item.item_name:
            shopify_product.title = cstr(erpnext_item.item_name).strip()
        
        if hasattr(erpnext_item, 'description') and erpnext_item.description:
            shopify_product.body_html = cstr(erpnext_item.description)
        
        if hasattr(erpnext_item, 'item_group') and erpnext_item.item_group:
            shopify_product.product_type = cstr(erpnext_item.item_group)

        # Map custom metafields
        map_product_meta_fields(shopify_product, erpnext_item)

        # Handle weight mapping
        if (hasattr(erpnext_item, 'weight_uom') and 
            hasattr(erpnext_item, 'weight_per_unit') and
            erpnext_item.weight_uom and 
            erpnext_item.weight_per_unit):
            
            try:
                if erpnext_item.weight_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
                    # Get Shopify weight UOM
                    uom = get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom)
                    if uom:
                        shopify_product.weight = float(erpnext_item.weight_per_unit)
                        shopify_product.weight_unit = uom
            except (ValueError, TypeError) as e:
                frappe.log_error(
                    "Weight Mapping Error",
                    f"Error mapping weight: {str(e)}\nWeight: {erpnext_item.weight_per_unit}\nUOM: {erpnext_item.weight_uom}"
                )

        # Handle product status
        if hasattr(erpnext_item, 'disabled'):
            if erpnext_item.disabled:
                shopify_product.status = "draft"
                shopify_product.published = False
                frappe.msgprint(_("Status of linked Shopify product is changed to Draft."))
            else:
                # Optionally set to active if not disabled
                shopify_product.status = "active"
                shopify_product.published = True

        frappe.log_error(
            "Product Mapping Completed",
            f"Successfully mapped ERPNext item {erpnext_item.get('name', 'Unknown')} to Shopify product"
        )

    except Exception as e:
        frappe.log_error(
            "Product Mapping Error",
            f"Error mapping ERPNext item to Shopify: {str(e)}\n\n{frappe.get_traceback()}"
        )
        frappe.throw(_("Failed to map product data: {0}").format(str(e)))


def validate_shopify_product_data(shopify_product, erpnext_item):
    """Validate the mapped Shopify product data before saving."""
    
    errors = []
    
    # Check required fields
    if not getattr(shopify_product, 'title', None):
        errors.append("Product title is required")
    
    # Validate weight if present
    if hasattr(shopify_product, 'weight') and shopify_product.weight:
        try:
            weight = float(shopify_product.weight)
            if weight < 0:
                errors.append("Weight cannot be negative")
        except (ValueError, TypeError):
            errors.append("Invalid weight value")
    
    # Log validation results
    if errors:
        error_msg = "Product validation failed: " + "; ".join(errors)
        frappe.log_error("Shopify Product Validation Error", error_msg)
        return False, errors
    
    return True, []


# Helper function to safely get metafield values
def get_metafield_value(shopify_product, namespace, key):
    """Safely retrieve metafield value from Shopify product."""
    try:
        if hasattr(shopify_product, 'metafields') and shopify_product.metafields:
            for metafield in shopify_product.metafields:
                if (getattr(metafield, 'namespace', None) == namespace and 
                    getattr(metafield, 'key', None) == key):
                    return getattr(metafield, 'value', None)
    except Exception as e:
        frappe.log_error(f"Error retrieving metafield {namespace}.{key}", str(e))
    
    return None


# Usage example with error handling
def update_shopify_product_safely(shopify_product, erpnext_item):
    """Safely update Shopify product with comprehensive error handling."""
    
    try:
        # Validate input data
        if not shopify_product:
            frappe.throw(_("Shopify product object is required"))
        
        if not erpnext_item:
            frappe.throw(_("ERPNext item data is required"))
        
        # Map the data
        map_erpnext_item_to_shopify(shopify_product, erpnext_item)
        
        # Validate the mapped data
        is_valid, validation_errors = validate_shopify_product_data(shopify_product, erpnext_item)
        
        if not is_valid:
            frappe.log_error("Product Validation Failed", "; ".join(validation_errors))
            frappe.throw(_("Product validation failed: {0}").format("; ".join(validation_errors)))
        
        # Save the product
        result = shopify_product.save()
        
        if result:
            frappe.msgprint(_("Shopify product updated successfully"))
            return True
        else:
            frappe.throw(_("Failed to save Shopify product"))
            
    except Exception as e:
        frappe.log_error(
            "Shopify Product Update Error",
            f"Critical error updating Shopify product: {str(e)}\n\n{frappe.get_traceback()}"
        )
        frappe.throw(_("Failed to update Shopify product: {0}").format(str(e)))
        
    return False


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
    for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
        if erpnext_uom == erpnext_weight_uom:
            return shopify_uom


def update_default_variant_properties(
    shopify_product: Product,
    is_stock_item: bool,
    sku: Optional[str] = None,
    price: Optional[float] = None,
):
    """Shopify creates default variant upon saving the product.

    Some item properties are supposed to be updated on the default variant.
    Input: saved shopify_product, sku and price
    """
    default_variant: Variant = shopify_product.variants[0]

    # this will create Inventory item and qty will be updated by scheduled job.
    if is_stock_item:
        default_variant.inventory_management = "shopify"

    if price is not None:
        default_variant.price = price
    if sku is not None:
        default_variant.sku = sku


def write_upload_log(status: bool, product: Product, item, action="Created") -> None:
    if not status:
        msg = _("Failed to upload item to Shopify") + "<br>"
        msg += (
            _("Shopify reported errors:")
            + " "
            + ", ".join(product.errors.full_messages())
        )
        msgprint(msg, title="Note", indicator="orange")

        create_shopify_log(
            status="Error",
            request_data=product.to_dict(),
            message=msg,
            method="upload_erpnext_item",
        )
    else:
        create_shopify_log(
            status="Success",
            request_data=product.to_dict(),
            message=f"{action} Item: {item.name}, shopify product: {product.id}",
            method="upload_erpnext_item",
        )
