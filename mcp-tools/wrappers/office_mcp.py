"""LibreOffice + Pandoc MCP Server。

封装两个底层命令行工具为统一的 MCP 服务，提供文档格式转换能力：
- LibreOffice headless：原生 Office 格式互转（docx/xlsx/pptx <-> pdf/html/...）
- Pandoc：Markdown/HTML/Word 等互转，Markdown 生态友好

契约：
- 输入/输出路径必须是绝对路径
- 输出目录不存在时自动创建
- 每次转换返回 output_path + bytes_written
- 单文件大小限制：100 MB

启动方式（stdio）：
    PYTHONPATH=/root/yyy/mcp-tools/pip python -m office_mcp

启动方式（HTTP）：
    PYTHONPATH=/root/yyy/mcp-tools/pip python -m office_mcp --transport streamable-http --port 8109
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("office-mcp")

MAX_BYTES = 100 * 1024 * 1024  # 100 MB
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice") or "/usr/bin/soffice"
PANDOC = shutil.which("pandoc")  # 可能为 None，pandoc 未装时 pandoc_convert 抛错


def _validate_path(p: str, *, must_exist: bool = True) -> Path:
    """规范化路径并校验：必须绝对路径；存在性按 must_exist 校验。"""
    path = Path(p).expanduser().resolve()
    if not path.is_absolute():
        raise ValueError(f"path must be absolute: {p}")
    if must_exist and not path.exists():
        raise FileNotFoundError(f"input not found: {path}")
    if path.exists() and path.stat().st_size > MAX_BYTES:
        raise ValueError(f"file too large (>{MAX_BYTES // 1024 // 1024}MB): {path}")
    return path


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _ext(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


# LibreOffice 支持的过滤器和目标格式后缀映射
# 参考 https://wiki.documentfoundation.org/Development/Filter
_LO_FILTERS: dict[str, str] = {
    "pdf": "writer_pdf_Export",  # Writer 类（docx/doc/rtf/odt）默认走 Writer
    "html": "HTML (StarWriter)",
    "odt": "writer8",
    "docx": "MS Word 2007 XML",
    "txt": "Text",
    "csv": "Text - txt - csv (StarCalc)",
    "xlsx": "Calc MS Excel 2007 XML",
    "ods": "Calc8",
    "pptx": "Impress MS PowerPoint 2007 XML",
    "odp": "Impress8",
    "png": "writer_png_Export",
}


@mcp.tool
async def libreoffice_convert(
    input_path: str,
    output_path: str,
    output_format: str | None = None,
) -> dict[str, Any]:
    """使用 LibreOffice headless 转换 Office 文档格式。

    支持的输入：docx/doc/odt/rtf/xlsx/xls/ods/csv/pptx/ppt/odp
    支持的输出（按后缀推断）：pdf/html/odt/docx/txt/csv/xlsx/ods/pptx/odp/png

    Args:
        input_path: 输入文件的绝对路径
        output_path: 输出文件的绝对路径；后缀决定目标格式
        output_format: 显式指定过滤器名（高级用法，一般留空让后缀推断）

    Returns:
        {"output_path": str, "bytes_written": int, "format": str}
    """
    src = _validate_path(input_path, must_exist=True)
    dst = _validate_path(output_path, must_exist=False)
    _ensure_parent(dst)
    target_ext = _ext(dst) if output_format is None else output_format.lower().lstrip(".")
    if target_ext not in _LO_FILTERS and output_format is None:
        raise ValueError(
            f"unsupported output extension '{target_ext}'; supported: {sorted(_LO_FILTERS)}"
        )
    filter_name = output_format or _LO_FILTERS.get(target_ext, _LO_FILTERS["pdf"])

    # soffice --headless --convert-to <filter>:<ext> --outdir <dir> <input>
    # 不能直接指定输出文件名，需用 --outdir + 重命名
    tmp_outdir = dst.parent / f".lo-tmp-{os.getpid()}-{dst.stem}"
    tmp_outdir.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            SOFFICE,
            "--headless",
            "--norestore",
            "--convert-to",
            f"{filter_name}:{target_ext}",
            "--outdir",
            str(tmp_outdir),
            str(src),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"soffice exit {proc.returncode}: {stderr.decode('utf-8', 'replace')}"
            )
        # soffice 输出文件名 = 输入 stem + .<target_ext>
        produced = tmp_outdir / f"{src.stem}.{target_ext}"
        if not produced.exists():
            # 某些版本不加后缀，回退查找第一个文件
            produced = next(tmp_outdir.iterdir())
        shutil.move(str(produced), str(dst))
    finally:
        shutil.rmtree(str(tmp_outdir), ignore_errors=True)

    bytes_written = dst.stat().st_size
    return {
        "output_path": str(dst),
        "bytes_written": bytes_written,
        "format": target_ext,
    }


@mcp.tool
async def pandoc_convert(
    input_path: str,
    output_path: str,
    from_format: str | None = None,
    to_format: str | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """使用 Pandoc 转换文档（Markdown/HTML/Word/LaTeX/EPUB 等）。

    需要系统已安装 pandoc（apt install pandoc 或 conda install -c conda-forge pandoc）。

    Args:
        input_path: 输入文件绝对路径
        output_path: 输出文件绝对路径（后缀推断默认 to_format）
        from_format: 显式输入格式（如 markdown、html、docx）；None=自动推断
        to_format: 显式输出格式；None=按 output_path 后缀推断
        extra_args: 透传给 pandoc 的额外命令行参数

    Returns:
        {"output_path": str, "bytes_written": int, "from": str, "to": str}
    """
    if PANDOC is None:
        raise RuntimeError(
            "pandoc not installed; install with `apt install pandoc` or "
            "`conda install -c conda-forge pandoc`"
        )
    src = _validate_path(input_path, must_exist=True)
    dst = _validate_path(output_path, must_exist=False)
    _ensure_parent(dst)
    src_fmt = from_format or _ext(src) or "markdown"
    dst_fmt = to_format or _ext(dst) or "markdown"

    cmd = [PANDOC, "-f", src_fmt, "-t", dst_fmt, "-o", str(dst)]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(src))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc exit {proc.returncode}: {stderr.decode('utf-8', 'replace')}"
        )
    bytes_written = dst.stat().st_size
    return {
        "output_path": str(dst),
        "bytes_written": bytes_written,
        "from": src_fmt,
        "to": dst_fmt,
    }


@mcp.tool
async def libreoffice_version() -> dict[str, str]:
    """获取 LibreOffice 版本，验证 wrapper 可用性。"""
    proc = await asyncio.create_subprocess_exec(
        SOFFICE,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return {
        "soffice": SOFFICE,
        "version": stdout.decode("utf-8", "replace").strip(),
    }


@mcp.tool
async def pandoc_version() -> dict[str, str]:
    """获取 Pandoc 版本（未安装则返回 not_installed）。"""
    if PANDOC is None:
        return {"pandoc": "not_installed"}
    proc = await asyncio.create_subprocess_exec(
        PANDOC,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return {
        "pandoc": PANDOC,
        "version": stdout.decode("utf-8", "replace").splitlines()[0] if stdout else "",
    }


if __name__ == "__main__":
    import os
    import sys

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8109"))
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run()
