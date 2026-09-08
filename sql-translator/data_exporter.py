# -*- coding: utf-8 -*-
"""
数据导出工具
功能：将查询结果导出为Excel并上传到MinIO，返回访问URL
"""

import os
import uuid
import tempfile
from typing import List, Dict, Any, Optional

from minio import Minio
from minio.error import S3Error
from runtime_config import env_bool, load_workspace_env

load_workspace_env()

# MinIO 配置（与 office_convert_service.py 保持一致）
MINIO_CONFIG = {
    "endpoint": os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000")
        .removeprefix("http://").removeprefix("https://").rstrip("/"),
    "access_key": os.getenv("MINIO_ACCESS_KEY", ""),
    "secret_key": os.getenv("MINIO_SECRET_KEY", ""),
    "bucket_name": os.getenv("SQL_EXPORT_MINIO_BUCKET")
        or os.getenv("AGENT_MINIO_BUCKET", "bam"),
    "secure": env_bool(
        "MINIO_SECURE",
        os.getenv("MINIO_ENDPOINT", "").strip().lower().startswith("https://"),
    ),
}

# MinIO 公网访问基础 URL
MINIO_PUBLIC_BASE = (
    os.getenv("MINIO_PUBLIC_ENDPOINT")
    or f"{'https' if MINIO_CONFIG['secure'] else 'http'}://{MINIO_CONFIG['endpoint']}"
).rstrip("/") + f"/{MINIO_CONFIG['bucket_name']}"

# 大数据量阈值（超过此条数导出为Excel）
EXPORT_THRESHOLD = 200
DOWNLOAD_PREVIEW_ROWS = 20


class DataExporter:
    """数据导出器：生成Excel并上传到MinIO"""

    def __init__(self):
        self.minio_client = Minio(
            endpoint=MINIO_CONFIG["endpoint"],
            access_key=MINIO_CONFIG["access_key"],
            secret_key=MINIO_CONFIG["secret_key"],
            secure=MINIO_CONFIG["secure"],
        )
        self.bucket_name = MINIO_CONFIG["bucket_name"]
        self._ensure_bucket()

    def _ensure_bucket(self):
        """确保bucket存在"""
        try:
            if not self.minio_client.bucket_exists(self.bucket_name):
                self.minio_client.make_bucket(self.bucket_name)
        except Exception as e:
            print(f"[EXPORTER-WARN] 检查bucket失败（可能已存在）: {e}")

    def export_to_excel(self, columns: List[str], data: List[Dict[str, Any]], 
                        sheet_name: str = "查询结果") -> str:
        """
        将数据导出为Excel文件并上传到MinIO

        Args:
            columns: 列名列表
            data: 查询结果数据（字典列表）
            sheet_name: 工作表名称

        Returns:
            str: Excel文件的完整访问URL
        """
        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment, PatternFill
        except ImportError:
            raise ImportError("缺少openpyxl库，请安装: pip install openpyxl")

        temp_dir = tempfile.mkdtemp(prefix="sql_export_")
        local_path = ""

        try:
            # 创建Excel工作簿
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = sheet_name[:31]  # Excel sheet名最长31字符

            # 表头样式
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
            header_alignment = Alignment(horizontal="center", vertical="center")

            # 写入表头
            for col_idx, col_name in enumerate(columns, 1):
                cell = ws.cell(row=1, column=col_idx, value=col_name)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment

            # 写入数据
            for row_idx, row_data in enumerate(data, 2):
                for col_idx, col_name in enumerate(columns, 1):
                    value = row_data.get(col_name, "")
                    ws.cell(row=row_idx, column=col_idx, value=value)

            # 自动调整列宽
            for col_idx, col_name in enumerate(columns, 1):
                max_length = len(str(col_name))
                for row in data:
                    val = str(row.get(col_name, ""))
                    if len(val) > max_length:
                        max_length = len(val)
                # 中文字符宽度约为英文2倍，简单估算
                adjusted_width = min(max_length * 2 + 2, 50)
                ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = adjusted_width

            # 冻结首行
            ws.freeze_panes = "A2"

            # 保存文件
            file_id = uuid.uuid4().hex
            file_name = f"{file_id}.xlsx"
            local_path = os.path.join(temp_dir, file_name)
            wb.save(local_path)

            # 上传到MinIO
            self.minio_client.fput_object(
                self.bucket_name,
                file_name,
                local_path,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            file_url = f"{MINIO_PUBLIC_BASE}/{file_name}"
            print(f"[EXPORTER] 已导出Excel: {file_name}, 行数: {len(data)}")
            return file_url

        finally:
            # 清理临时文件
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            try:
                os.rmdir(temp_dir)
            except Exception:
                pass


# 单例实例
_exporter = None


def get_exporter() -> DataExporter:
    """获取数据导出器单例"""
    global _exporter
    if _exporter is None:
        _exporter = DataExporter()
    return _exporter


def format_query_result(columns: List[str], data: List[Dict[str, Any]], 
                       row_count: int, sql: str = None) -> Dict[str, Any]:
    """
    格式化查询结果，根据数据量决定直接返回JSON还是导出Excel

    Args:
        columns: 列名列表
        data: 查询结果数据
        row_count: 行数
        sql: 生成的SQL语句（可选）

    Returns:
        dict: {
            "success": True,
            "sql": "...",
            "row_count": N,
            "columns": [...],
            "data": [...],           # 数据量小时返回
            "download_url": "..."     # 数据量大时返回（Excel下载链接）
        }
    """
    result = {
        "success": True,
        "sql": sql,
        "row_count": row_count,
    }

    if row_count > EXPORT_THRESHOLD:
        # 数据量大，导出Excel
        try:
            exporter = get_exporter()
            download_url = exporter.export_to_excel(columns, data)
            result["columns"] = columns
            # Keep the normal small-result shape for downstream consumers:
            # ``columns`` plus a list in ``data``.  The workbook still contains
            # the complete result, while chat/data agents can render an inline
            # preview without downloading and parsing the file again.
            result["data"] = data[:DOWNLOAD_PREVIEW_ROWS]
            result["download_url"] = download_url
            result["preview_count"] = min(len(data), DOWNLOAD_PREVIEW_ROWS)
            result["preview_truncated"] = row_count > len(result["data"])
            result["message"] = (
                f"数据量({row_count}条)超过{EXPORT_THRESHOLD}条，已导出为Excel；"
                f"data字段返回前{len(result['data'])}条预览，"
                "完整结果请通过download_url下载"
            )
        except Exception as e:
            # 导出失败时降级返回原始数据
            result["columns"] = columns
            result["data"] = data
            result["download_url"] = None
            result["export_error"] = f"Excel导出失败: {str(e)}，返回原始JSON数据"
    else:
        # 数据量小，直接返回
        result["columns"] = columns
        result["data"] = data
        result["download_url"] = None

    return result
