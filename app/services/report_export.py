"""Deterministically export a scoped dataset to XLSX, DOCX or PDF in MinIO."""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape

from minio_followup_store import DatasetReference, DatasetScope, HybridMinioFollowupStore


class ReportExportError(ValueError):
    pass


class DatasetReportExporter:
    def __init__(
        self, minio_client, dataset_store: HybridMinioFollowupStore, *,
        bucket: str, object_ttl_seconds: int = 86_400,
    ):
        self.client = minio_client
        self.dataset_store = dataset_store
        self.bucket = bucket
        self.object_ttl_seconds = object_ttl_seconds

    @staticmethod
    def _scope_prefix(scope: DatasetScope) -> str:
        raw = "\x1f".join((scope.tenant_id, scope.user_id, scope.application_id)).encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    @staticmethod
    def _text(value: Any) -> str:
        text = "" if value is None else str(value)
        # XML-based XLSX/DOCX and ReportLab cannot safely represent most C0
        # controls. Tabs/newlines remain useful inside report cells.
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

    @classmethod
    def _xlsx_text(cls, value: Any) -> str:
        text = cls._text(value)
        # Prevent spreadsheet-formula injection when exported values originate
        # from uploaded files or database text. The apostrophe is Excel's
        # standard literal-text escape and is not displayed in the cell UI.
        if text.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    def _xlsx(self, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], title: str) -> bytes:
        from openpyxl import Workbook

        # Write-only mode keeps XLSX export memory bounded for medium datasets.
        workbook = Workbook(write_only=True)
        summary = workbook.create_sheet("说明")
        summary.append(["报表标题", self._xlsx_text(title)])
        summary.append(["生成时间", datetime.now(timezone.utc).isoformat()])
        summary.append(["数据行数", len(rows)])
        sheet = workbook.create_sheet("数据")
        sheet.append([self._xlsx_text(column) for column in columns])
        for row in rows:
            sheet.append([self._xlsx_text(row.get(column)) for column in columns])
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()

    def _docx(self, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], title: str) -> bytes:
        from docx import Document

        document = Document()
        document.add_heading(self._text(title), level=1)
        document.add_paragraph(f"生成时间：{datetime.now(timezone.utc).isoformat()}")
        document.add_paragraph(f"数据行数：{len(rows)}；字段数：{len(columns)}")
        preview = rows[:200]
        table = document.add_table(rows=1, cols=len(columns))
        table.style = "Table Grid"
        for index, column in enumerate(columns):
            table.rows[0].cells[index].text = column
        for row in preview:
            cells = table.add_row().cells
            for index, column in enumerate(columns):
                cells[index].text = self._text(row.get(column))[:1000]
        if len(rows) > len(preview):
            document.add_paragraph("Word预览仅展示前200行，完整数据请下载Excel报表。")
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()

    def _pdf(self, columns: Sequence[str], rows: Sequence[Mapping[str, Any]], title: str) -> bytes:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        output = io.BytesIO()
        document = SimpleDocTemplate(output, pagesize=landscape(A4))
        styles = getSampleStyleSheet()
        styles["Title"].fontName = "STSong-Light"
        styles["BodyText"].fontName = "STSong-Light"
        story = [Paragraph(escape(self._text(title)), styles["Title"]), Spacer(1, 12)]
        story.append(Paragraph(f"数据行数：{len(rows)}；字段数：{len(columns)}", styles["BodyText"]))
        display_columns = list(columns[:12])
        data = [display_columns]
        for row in rows[:200]:
            data.append([self._text(row.get(column))[:80] for column in display_columns])
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.extend([Spacer(1, 12), table])
        document.build(story)
        return output.getvalue()

    def _xlsx_many(
        self,
        sections: Sequence[tuple[str, Sequence[str], Sequence[Mapping[str, Any]], DatasetReference]],
        title: str,
    ) -> bytes:
        from openpyxl import Workbook

        workbook = Workbook(write_only=True)
        summary = workbook.create_sheet("报告说明")
        summary.append(["报表标题", self._xlsx_text(title)])
        summary.append(["生成时间", datetime.now(timezone.utc).isoformat()])
        summary.append(["章节数", len(sections)])
        summary.append(["章节", "数据集ID", "数据行数", "数据截至时间", "快照ID"])
        for index, (section_title, _columns, rows, reference) in enumerate(sections, 1):
            summary.append([
                self._xlsx_text(section_title), reference.dataset_id, len(rows),
                reference.data_as_of, reference.snapshot_id,
            ])
            sheet = workbook.create_sheet(f"章节{index}")
            sheet.append(["章节", self._xlsx_text(section_title)])
            sheet.append(["数据集ID", reference.dataset_id])
            sheet.append([])
            sheet.append([self._xlsx_text(column) for column in _columns])
            for row in rows:
                sheet.append([self._xlsx_text(row.get(column)) for column in _columns])
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()

    def _docx_many(
        self,
        sections: Sequence[tuple[str, Sequence[str], Sequence[Mapping[str, Any]], DatasetReference]],
        title: str,
    ) -> bytes:
        from docx import Document

        document = Document()
        document.add_heading(self._text(title), level=1)
        document.add_paragraph(
            f"生成时间：{datetime.now(timezone.utc).isoformat()}；章节数：{len(sections)}"
        )
        for index, (section_title, columns, rows, reference) in enumerate(sections, 1):
            document.add_heading(f"{index}. {self._text(section_title)}", level=2)
            document.add_paragraph(
                f"数据集：{reference.dataset_id}；数据行数：{len(rows)}；"
                f"数据截至：{reference.data_as_of}"
            )
            if not columns:
                document.add_paragraph("本章节没有可展示字段。")
                continue
            preview = rows[:200]
            table = document.add_table(rows=1, cols=len(columns))
            table.style = "Table Grid"
            for column_index, column in enumerate(columns):
                table.rows[0].cells[column_index].text = self._text(column)
            for row in preview:
                cells = table.add_row().cells
                for column_index, column in enumerate(columns):
                    cells[column_index].text = self._text(row.get(column))[:1000]
            if len(rows) > len(preview):
                document.add_paragraph(
                    "本章节Word预览仅展示前200行；完整章节数据请导出Excel。"
                )
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()

    def _pdf_many(
        self,
        sections: Sequence[tuple[str, Sequence[str], Sequence[Mapping[str, Any]], DatasetReference]],
        title: str,
    ) -> bytes:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        output = io.BytesIO()
        document = SimpleDocTemplate(output, pagesize=landscape(A4))
        styles = getSampleStyleSheet()
        styles["Title"].fontName = "STSong-Light"
        styles["Heading2"].fontName = "STSong-Light"
        styles["BodyText"].fontName = "STSong-Light"
        story = [Paragraph(escape(self._text(title)), styles["Title"]), Spacer(1, 12)]
        for index, (section_title, columns, rows, reference) in enumerate(sections, 1):
            if index > 1:
                story.append(PageBreak())
            story.append(Paragraph(
                escape(f"{index}. {self._text(section_title)}"), styles["Heading2"]
            ))
            story.append(Paragraph(
                escape(
                    f"数据集：{reference.dataset_id}；数据行数：{len(rows)}；"
                    f"数据截至：{reference.data_as_of}"
                ),
                styles["BodyText"],
            ))
            display_columns = list(columns[:12])
            if not display_columns:
                story.append(Paragraph("本章节没有可展示字段。", styles["BodyText"]))
                continue
            data = [display_columns]
            for row in rows[:200]:
                data.append([self._text(row.get(column))[:80] for column in display_columns])
            table = Table(data, repeatRows=1)
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.extend([Spacer(1, 12), table])
            if len(rows) > 200:
                story.append(Paragraph(
                    "本章节PDF预览仅展示前200行；完整章节数据请导出Excel。",
                    styles["BodyText"],
                ))
        document.build(story)
        return output.getvalue()

    def _publish(
        self,
        payload: bytes,
        *,
        file_format: str,
        scope: DatasetScope,
        dataset_ids: Sequence[str],
    ) -> dict[str, Any]:
        report_id = f"report-{uuid.uuid4()}"
        object_name = (
            f"data-analysis/reports/{self._scope_prefix(scope)}/"
            f"{report_id}.{file_format}"
        )
        content_types = {
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pdf": "application/pdf",
        }
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=self.object_ttl_seconds)
        checksum = hashlib.sha256(payload).hexdigest()
        self.client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(payload),
            len(payload),
            content_type=content_types[file_format],
            metadata={
                "report-id": report_id,
                "expires-at": expires_at.isoformat(),
                "content-sha256": checksum,
                "dataset-count": str(len(dataset_ids)),
            },
        )
        url = self.client.presigned_get_object(
            self.bucket, object_name, expires=timedelta(hours=1)
        )
        return {
            "report_id": report_id,
            "format": file_format,
            "object_name": object_name,
            "download_url": url,
            "byte_size": len(payload),
            "expires_in_seconds": 3600,
            "object_expires_at": expires_at.isoformat(),
            "dataset_ids": list(dataset_ids),
            "report_reference": {
                "report_id": report_id,
                "bucket": self.bucket,
                "object_name": object_name,
                "scope": {
                    "tenant_id": scope.tenant_id,
                    "user_id": scope.user_id,
                    "application_id": scope.application_id,
                    "conversation_id": scope.conversation_id,
                },
                "dataset_ids": list(dataset_ids),
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "content_sha256": checksum,
            },
        }

    def export(
        self,
        reference: DatasetReference,
        *,
        scope: DatasetScope,
        file_format: str,
        title: str,
    ) -> dict[str, Any]:
        file_format = file_format.lower()
        if file_format not in {"xlsx", "docx", "pdf"}:
            raise ReportExportError("format must be xlsx, docx or pdf")
        loaded = self.dataset_store.load_dataset(reference, current_scope=scope)
        rows = loaded.rows
        columns = list(loaded.reference.columns)
        builders = {"xlsx": self._xlsx, "docx": self._docx, "pdf": self._pdf}
        payload = builders[file_format](columns, rows, title)
        return self._publish(
            payload,
            file_format=file_format,
            scope=scope,
            dataset_ids=[reference.dataset_id],
        )

    def export_many(
        self,
        sections: Sequence[tuple[str, DatasetReference]],
        *,
        scope: DatasetScope,
        file_format: str,
        title: str,
    ) -> dict[str, Any]:
        """Export independently queried report sections without flattening grains."""
        file_format = file_format.lower()
        if file_format not in {"xlsx", "docx", "pdf"}:
            raise ReportExportError("format must be xlsx, docx or pdf")
        if not 2 <= len(sections) <= 5:
            raise ReportExportError("composite report requires two to five datasets")
        loaded_sections = []
        dataset_ids: list[str] = []
        for section_title, reference in sections:
            if (
                reference.scope.tenant_id != scope.tenant_id
                or reference.scope.user_id != scope.user_id
                or reference.scope.application_id != scope.application_id
            ):
                raise ReportExportError(
                    "composite report datasets must belong to the same tenant, user and application"
                )
            loaded = self.dataset_store.load_dataset(
                reference, current_scope=reference.scope
            )
            loaded_sections.append((
                section_title,
                list(loaded.reference.columns),
                loaded.rows,
                loaded.reference,
            ))
            dataset_ids.append(reference.dataset_id)
        builders = {
            "xlsx": self._xlsx_many,
            "docx": self._docx_many,
            "pdf": self._pdf_many,
        }
        payload = builders[file_format](loaded_sections, title)
        return self._publish(
            payload,
            file_format=file_format,
            scope=scope,
            dataset_ids=dataset_ids,
        )

    def delete_object(self, object_name: str) -> None:
        if not object_name.startswith("data-analysis/reports/"):
            raise ReportExportError("invalid report object path")
        self.client.remove_object(self.bucket, object_name)
