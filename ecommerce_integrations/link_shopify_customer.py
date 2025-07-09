import frappe
import requests


@frappe.whitelist()
def link_shopify_customers():
    customers = frappe.db.sql(
        "SELECT name, email_id FROM `tabCustomer` where shopify_customer_id IS NULL AND email_id IS NOT NULL",
        as_dict=1,
    )
    settings = frappe.get_doc("Shopify Setting", "Shopify Setting")
    ACCESS_TOKEN = settings.get_password("password")
    if not ACCESS_TOKEN:
        return

    API_URL = f'https://{settings.get("shopify_url")}/admin/api/2025-04/graphql.json'
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": ACCESS_TOKEN,
    }
    graphql_query = """query getCustomersByEmail($searchQuery: String!) {
      customers(first: 10, query: $searchQuery) {
        nodes {
          id
          email
          firstName
          lastName
        }
      }
    }"""

    for i in customers:
        customer_email = i.get("email_id")
        variables = {"searchQuery": f"email:{customer_email}"}
        payload = {"query": graphql_query, "variables": variables}
        response = requests.post(API_URL, headers=headers, json=payload)
        data = response.json().get("data", {}).get("customers").get("nodes", [])

        for i in data:
            shopify_id = str(i.get("id", "")).split("/")[-1]

            if i.get("email") == customer_email and shopify_id:
                frappe.db.set_value(
                    "Customer", {"email_id": customer_email}, {"shopify_customer_id": shopify_id}
                )
                frappe.db.commit()
