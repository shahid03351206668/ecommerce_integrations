import boto3
import frappe
from frappe.utils.pdf import get_pdf
from frappe.utils.file_manager import save_file
import os
from frappe.utils.file_manager import get_file_path
from botocore.exceptions import ClientError, NoCredentialsError
import mimetypes


def sales_invoice_on_submit(self, method=None):
    try:
        print_format = "Virima SI"
        html = frappe.get_print(
            "Sales Invoice",
            self.name,
            print_format,
            doc=self,
        )
        pdf_data = get_pdf(html)

        file = save_file(
            fname=self.get("shopify_order_id") + ".pdf",
            content=pdf_data,
            dt="Sales Invoice",
            dn=self.name,
            decode=False,
            is_private=True,
        )
        file.save()
    except Exception as e:
        frappe.log_error(
            f"Error generating PDF for Sales Invoice {self.name}: {str(e)}",
            "Sales Invoice PDF Generation Error",
        )


def main():
    posted_invoices = frappe.db.sql(
        "SELECT name, shopify_order_id FROM `tabSales Invoice` WHERE docstatus = 1 and name NOT IN (SELECT f.attached_to_name FROM `tabFile` f WHERE f.file_type = 'PDF' and f.attached_to_doctype = 'Sales Invoice')",
        as_dict=True,
    )

    print_format = "Virima SI"
    for invoice in posted_invoices:
        try:
            doc = frappe.get_doc("Sales Invoice", invoice.name)
            html = frappe.get_print(
                "Sales Invoice",
                invoice.name,
                print_format,
                doc=doc,
            )
            pdf_data = get_pdf(html)

            file = save_file(
                fname=invoice.get("shopify_order_id") + ".pdf",
                content=pdf_data,
                dt="Sales Invoice",
                dn=invoice.name,
                decode=False,
                is_private=True,
            )
            file.save()

        except Exception as e:
            frappe.log_error(
                f"Error processing invoice {invoice.name}", frappe.get_traceback()
            )


def sync_to_aws():
    try:
        doc = frappe.get_single("S3 Backup Settings")

        if not doc.access_key_id:
            frappe.throw("AWS Access Key ID is not configured in S3 Backup Settings")

        conn = boto3.client(
            "s3",
            aws_access_key_id=doc.access_key_id,
            aws_secret_access_key=doc.get_password("secret_access_key"),
            endpoint_url=doc.endpoint_url or "https://s3.amazonaws.com",
            region_name="us-east-1",
        )

        bucket = "virima-dokumentenarchiv"
        files = frappe.db.sql(
            """
            SELECT name, file_name, file_url, attached_to_doctype, attached_to_name, 
                   creation, modified, file_size
            FROM `tabFile` 
            WHERE file_type = 'PDF' 
            AND file_url IS NOT NULL 
            AND file_url != ''
            ORDER BY creation DESC
        """,
            as_dict=True,
        )

        if not files:
            frappe.msgprint("No PDF files found to sync")
            return

        successful_uploads = 0
        failed_uploads = 0
        skipped_uploads = 0

        for file_doc in files:
            try:
                file_path = get_file_path(file_doc.file_url)

                if not file_path or not os.path.exists(file_path):
                    failed_uploads += 1
                    continue

                if file_doc.attached_to_doctype and file_doc.attached_to_name:
                    s3_key = f"pdfs/{file_doc.attached_to_doctype.replace(' ', '_')}/{file_doc.attached_to_name.replace(' ', '_')}/{file_doc.file_name}"
                else:
                    s3_key = f"pdfs/unattached/{file_doc.file_name}"
                try:
                    conn.head_object(Bucket=bucket, Key=s3_key)
                    skipped_uploads += 1
                    continue
                except ClientError as e:
                    if e.response["Error"]["Code"] != "404":
                        raise e

                content_type, _ = mimetypes.guess_type(file_doc.file_name)
                if not content_type:
                    content_type = "application/pdf"

                extra_args = {
                    "ContentType": content_type,
                    "Metadata": {
                        "frappe_file_name": file_doc.name,
                        "attached_to_doctype": file_doc.attached_to_doctype or "",
                        "attached_to_name": file_doc.attached_to_name or "",
                        "creation_date": str(file_doc.creation),
                        "file_size": str(file_doc.file_size or 0),
                    },
                }

                conn.upload_file(
                    Filename=file_path, Bucket=bucket, Key=s3_key, ExtraArgs=extra_args
                )

                successful_uploads += 1

                # s3_url = f"https://{bucket}.s3.amazonaws.com/{s3_key}"
                # frappe.db.set_value("File", file_doc.name, "s3_url", s3_url)

            except ClientError as e:
                error_msg = f"AWS Error uploading {file_doc.file_name}: {e}"
                failed_uploads += 1

            except Exception as e:
                error_msg = f"Error uploading {file_doc.file_name}: {str(e)}"
                failed_uploads += 1

        total_files = len(files)

        summary_msg = f"""
        📊 AWS PDF Sync Summary:
        Total files processed: {total_files}
        ✅ Successful uploads: {successful_uploads}
        ⏭️ Skipped (already exist): {skipped_uploads}
        ❌ Failed uploads: {failed_uploads}
        """

        frappe.log_error(summary_msg, "AWS PDF Sync Summary")
        frappe.db.commit()

    except NoCredentialsError:
        frappe.log_error("AWS credentials not found or invalid")

    except Exception as e:
        frappe.log_error("AWS PDF Sync Error", str(e))
