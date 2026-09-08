#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SQL翻译器 HTTP API 服务（生产版）

接口列表：
  1. POST /api/translate   - 仅翻译ASL为SQL，不执行
  2. POST /api/execute     - 执行SQL并返回结果（>200条自动导出Excel到MinIO）
  3. POST /api/ast-to-sql  - 完整流程（翻译+执行），保持向后兼容
  4. POST /api/cache/refresh - 刷新DSL缓存
  5. GET  /api/health      - 健康检查

DSL 数据来源：由运行环境配置的 Redis。
"""

import json
import hashlib
import os
import sys
import re
import uuid
from urllib.parse import urlsplit, unquote
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# 将当前目录加入 sys.path，便于 import 同目录模块
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from sql_translator_prod import SQLTranslatorProd, _positive_int
from analysis_contract import normalize_contract, normalize_result, validate_result

# 日志目录与文件
LOG_DIR = os.path.join(CURRENT_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'api_server.log')


def _source_digest(filename: str) -> str:
    try:
        with open(os.path.join(CURRENT_DIR, filename), 'rb') as source:
            return hashlib.sha256(source.read()).hexdigest()[:16]
    except OSError:
        return 'unavailable'


BUILD_INFO = {
    'version': '2.1.0',
    'api_server_sha256': _source_digest('api_server_prod.py'),
    'translator_sha256': _source_digest('sql_translator_prod.py'),
}


def log(msg: str) -> None:
    """写日志：同时输出到控制台和日志文件（带时间戳）"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Logging must never turn a valid API request into HTTP 500 merely
        # because a Windows service console still uses a legacy code page.
        encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        safe_line = line.encode(encoding, errors='backslashreplace').decode(encoding)
        print(safe_line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


# 全局翻译器实例：复用 Redis 连接与缓存
_translator = None


def get_translator() -> SQLTranslatorProd:
    """惰性创建翻译器实例（单例）"""
    global _translator
    if _translator is None:
        _translator = SQLTranslatorProd()
    return _translator


def _read_request_body(handler) -> tuple:
    """读取并解析请求体，返回 (wrapper_dict, body_str, error_response_tuple)"""
    try:
        content_length = int(handler.headers.get('Content-Length', 0))
    except (TypeError, ValueError):
        content_length = 0
    body = handler.rfile.read(content_length).decode('utf-8') if content_length > 0 else ''
    log(f"[REQUEST] {handler.path} 入参: {body}")

    if content_length == 0:
        log("[ERROR] 请求体为空")
        return None, body, (400, {'success': False, 'error': '请求体为空'})

    try:
        wrapper = json.loads(body)
    except json.JSONDecodeError as e:
        log(f"[ERROR] JSON解析错误: {str(e)} | 入参: {body}")
        return None, body, (400, {'success': False, 'error': f"JSON解析错误: {str(e)}"})

    return wrapper, body, _scope_contract_error(wrapper)


def _scope_contract_error(wrapper):
    """Reject unsupported explicit grants before model-wide catalog/cache access.

    This translator currently has model-scoped catalog and Redis keys only.
    Accepting a domain claim while ignoring it would widen caller authorization.
    """
    def domains(source):
        values = source.get('business_domain_ids', [])
        if not isinstance(values, list) or any(type(v) is not int or v <= 0 for v in values):
            raise ValueError('business_domain_ids must contain strict positive integers')
        values = sorted(set(values))
        legacy = source.get('business_domain_id')
        if legacy is not None:
            if type(legacy) is not int or legacy <= 0:
                raise ValueError('business_domain_id must be a strict positive integer')
            if 'business_domain_ids' in source and values != [legacy]:
                raise ValueError('business domain fields conflict')
            values = [legacy]
        return values

    try:
        if not isinstance(wrapper, dict):
            raise ValueError('request body must be an object')
        requested = domains(wrapper)
        authorized = wrapper.get('authorized_semantic_scope')
        if authorized is not None:
            if not isinstance(authorized, dict):
                raise ValueError('authorized_semantic_scope must be an object')
            model = authorized.get('semantic_model_id')
            if type(model) is not int or model <= 0:
                raise ValueError('authorized model must be a strict positive integer')
            for name in ('semantic_model_id', 'modelId', 'model_id'):
                if name in wrapper and _positive_int(wrapper[name]) != model:
                    raise ValueError('request and authorized model disagree')
            granted = domains(authorized)
            expected_mode = 'EXPLICIT_DOMAINS' if granted else 'MODEL_WIDE'
            if authorized.get('scope_mode') != expected_mode:
                raise ValueError('authorized scope mode disagrees with domains')
            if any(name in wrapper for name in ('business_domain_id', 'business_domain_ids')) and requested != granted:
                raise ValueError('request and authorized domains disagree')
            requested = granted
        if requested:
            code = ('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED' if len(requested) > 1
                    else 'EXPLICIT_DOMAIN_NOT_SUPPORTED')
            return 400, {'success': False, 'code': code, 'retryable': False,
                         'error': 'Translator catalog and cache cannot enforce an explicit business domain yet'}
    except (TypeError, ValueError):
        return 400, {'success': False, 'code': 'REQUEST_SCOPE_INVALID', 'retryable': False,
                     'error': 'Semantic scope is invalid or inconsistent'}
    return None


def _extract_asl_and_model_id(wrapper: dict, body: str) -> tuple:
    """从请求中提取 asl_str 和 model_id，返回 (asl_str, model_id, error_response_tuple)"""
    asl_str = wrapper.get('asl')
    if asl_str is None:
        log(f"[ERROR] 缺少 asl 字段 | 入参: {body}")
        return None, None, (400, {'success': False, 'error': '缺少 asl 字段'})
    if not isinstance(asl_str, str):
        # 兼容直接传对象的情况：重新序列化为字符串再解析
        asl_str = json.dumps(asl_str, ensure_ascii=False)

    model_id = wrapper.get('modelId') or wrapper.get('model_id')
    if model_id is None:
        return None, None, (400, {
            'success': False, 'code': 'INVALID_REQUEST',
            'error': '缺少modelId', 'retryable': False,
        })
    try:
        model_id = str(_positive_int(model_id, 'modelId'))
    except ValueError as exc:
        return None, None, (400, {
            'success': False, 'code': 'INVALID_REQUEST',
            'error': str(exc), 'retryable': False,
        })
    return asl_str, model_id, None


class APIHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'  # HTTP/1.0 + Content-Length，兼容性最好

    def _send_response(self, status_code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        incoming = (self.headers.get('X-Request-Id') or '').strip()
        request_id = incoming if re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', incoming) else str(uuid.uuid4())
        self.send_header('X-Request-Id', request_id)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_response(200, {'status': 'ok'})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {'/api/health', '/health'}:
            self._send_response(200, {
                'status': 'ok',
                'message': 'SQL翻译器API服务（生产版）运行正常',
                'dsl_source': 'redis',
                'build': BUILD_INFO,
                'endpoints': {
                    'translate': 'POST /api/translate - 仅翻译ASL为SQL',
                    'execute': 'POST /api/execute - 执行SQL返回结果',
                    'ast_to_sql': 'POST /api/ast-to-sql - 完整流程（翻译+执行）',
                    'cache_refresh': 'POST /api/cache/refresh - 刷新DSL缓存',
                }
            })
        elif path == '/openapi.json':
            self._send_response(200, self._openapi_document())
        else:
            definition = re.fullmatch(r'/v1/semantic/metrics/([^/]+)/versions/([^/]+)', path)
            if definition:
                self._handle_metric_definition(unquote(definition.group(1)), unquote(definition.group(2)))
                return
            self._send_response(404, {
                'success': False, 'code': 'NOT_FOUND', 'error': 'Not found', 'retryable': False,
            })

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == '/api/translate':
            self._handle_translate()
        elif path == '/api/execute':
            self._handle_execute()
        elif path == '/api/ast-to-sql':
            self._handle_ast_to_sql()
        elif path == '/api/cache/refresh':
            self._handle_cache_refresh()
        elif path == '/v1/semantic/metrics/resolve':
            self._handle_metric_resolve()
        elif path == '/v1/semantic/relationships:resolve':
            self._handle_relationship_resolve()
        else:
            lineage = re.fullmatch(r'/v1/semantic/metrics/([^/]+)/lineage', path)
            if lineage:
                self._handle_metric_lineage(unquote(lineage.group(1)))
                return
            self._send_response(404, {
                'success': False, 'code': 'NOT_FOUND', 'error': 'Not found', 'retryable': False,
            })

    @staticmethod
    def _openapi_document():
        paths = {
            '/api/translate': {'post': {}},
            '/api/execute': {'post': {}},
            '/api/ast-to-sql': {'post': {}},
            '/api/cache/refresh': {'post': {}},
            '/v1/semantic/metrics/resolve': {'post': {}},
            '/v1/semantic/relationships:resolve': {'post': {}},
            '/v1/semantic/metrics/{metric_id}/versions/{version}': {'get': {}},
            '/v1/semantic/metrics/{metric_id}/lineage': {'post': {}},
        }
        return {'openapi': '3.0.3', 'info': {'title': 'SQL Translator', 'version': '2.1.0'}, 'paths': paths}

    def _request_id(self):
        incoming = (self.headers.get('X-Request-Id') or '').strip()
        return incoming if re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', incoming) else str(uuid.uuid4())

    def _handle_metric_resolve(self):
        wrapper, _body, err = _read_request_body(self)
        if err:
            self._send_response(*err)
            return
        try:
            result = get_translator().catalog.resolve_metrics(
                wrapper.get('semantic_model_id'), wrapper.get('metric_names')
            )
            result['request_id'] = self._request_id()
            self._send_response(200, result)
        except ValueError as exc:
            self._send_response(400, {'success': False, 'code': 'INVALID_REQUEST', 'error': str(exc), 'retryable': False})
        except Exception:
            self._send_response(503, {'success': False, 'code': 'SEMANTIC_CATALOG_UNAVAILABLE', 'error': '语义目录暂时不可用', 'retryable': True})

    def _handle_relationship_resolve(self):
        wrapper, _body, err = _read_request_body(self)
        if err:
            self._send_response(*err)
            return
        try:
            result = get_translator().loader.resolve_relationship_paths(
                wrapper.get('semantic_model_id'),
                str(wrapper.get('source_entity') or '').strip(),
                wrapper.get('target_entities') or [],
            )
            result['request_id'] = self._request_id()
            self._send_response(200, result)
        except (TypeError, ValueError) as exc:
            self._send_response(400, {'success': False, 'code': 'INVALID_RELATIONSHIP_REQUEST', 'error': str(exc), 'retryable': False})
        except Exception:
            self._send_response(503, {'success': False, 'code': 'SEMANTIC_RELATIONSHIP_UNAVAILABLE', 'error': '语义关系暂时不可用', 'retryable': True})

    def _handle_metric_definition(self, metric_id, version):
        try:
            result = get_translator().catalog.definition(metric_id, version)
            result['request_id'] = self._request_id()
            self._send_response(200, result)
        except ValueError as exc:
            self._send_response(404, {'success': False, 'code': 'METRIC_NOT_FOUND', 'error': str(exc), 'retryable': False})
        except Exception:
            self._send_response(503, {'success': False, 'code': 'SEMANTIC_CATALOG_UNAVAILABLE', 'error': '语义目录暂时不可用', 'retryable': True})

    def _handle_metric_lineage(self, metric_id):
        wrapper, _body, err = _read_request_body(self)
        if err:
            self._send_response(*err)
            return
        try:
            result = get_translator().catalog.lineage(metric_id, wrapper.get('version') or 'current')
            result['request_id'] = self._request_id()
            self._send_response(200, result)
        except ValueError as exc:
            self._send_response(404, {'success': False, 'code': 'METRIC_NOT_FOUND', 'error': str(exc), 'retryable': False})
        except Exception:
            self._send_response(503, {'success': False, 'code': 'SEMANTIC_CATALOG_UNAVAILABLE', 'error': '语义目录暂时不可用', 'retryable': True})

    def _handle_translate(self):
        """
        仅翻译接口：ASL -> SQL，不执行查询

        入参格式：{"asl": "<ASL的JSON字符串>", "modelId": "<模型ID>"}
        返回：{"success": true, "sql": "SELECT ...", "modelId": "..."}
        """
        try:
            wrapper, body, err = _read_request_body(self)
            if err:
                self._send_response(*err)
                return

            asl_str, model_id, err = _extract_asl_and_model_id(wrapper, body)
            if err:
                self._send_response(*err)
                return

            # 检查歧义
            try:
                ast_data = json.loads(asl_str)
            except json.JSONDecodeError as e:
                log(f"[ERROR] ASL解析错误: {str(e)} | 入参: {body}")
                self._send_response(400, {'success': False, 'error': f"ASL解析错误: {str(e)}"})
                return

            ambiguity = ast_data.get('ambiguity', [])
            if ambiguity:
                questions = [{
                    'type': amb.get('type', ''),
                    'question': amb.get('question', ''),
                    'candidates': amb.get('candidates', []),
                } for amb in ambiguity]
                log(f"[ERROR] DSL存在未解决的歧义: {questions}")
                self._send_response(400, {
                    'success': False,
                    'error': 'DSL存在未解决的歧义，请先确认后再查询',
                    'ambiguity': questions,
                })
                return

            embedded_contract = ast_data.get('analysis_contract')
            analysis_contract = wrapper.get('analysis_contract')
            if analysis_contract is None and isinstance(embedded_contract, dict):
                analysis_contract = embedded_contract.get('result_contract')
            try:
                analysis_contract = normalize_contract(analysis_contract)
            except ValueError as exc:
                self._send_response(400, {
                    'success': False, 'code': 'ANALYSIS_CONTRACT_INVALID',
                    'error': str(exc), 'retryable': False,
                })
                return

            # 调用翻译器仅翻译
            translator = get_translator()
            result = translator.translate_only(asl_str, model_id)

            if result['success']:
                log(f"[TRANSLATE] 成功: sql={result.get('sql')}")
                self._send_response(200, {
                    'success': True,
                    'sql': result['sql'],
                    'modelId': result.get('model_id'),
                    'dataSourceId': result.get('data_source_id'),
                    'analysis_contract': analysis_contract,
                    'contract_accepted': analysis_contract is not None,
                    'semantic_validation_report': result.get('semantic_validation_report'),
                })
            else:
                log(f"[TRANSLATE] 失败: error={result.get('error')}")
                # The request body is valid; return a stable business failure
                # instead of disguising it as service unavailability.
                self._send_response(200, result)

        except Exception as e:
            import traceback
            log(f"[ERROR] 翻译接口异常: {str(e)}\n{traceback.format_exc()}")
            self._send_response(500, {'success': False, 'error': f"服务器内部错误: {str(e)}"})

    def _handle_execute(self):
        """
        SQL执行接口：执行SQL -> 返回结果（>200条自动导出Excel到MinIO，同时预览前20条）

        入参格式：{"sql": "SELECT ...", "modelId": "<模型ID>", "dataSourceId": "<数据源ID>"}
        返回：
          - 数据<=200条：{"success": true, "data": [...], "columns": [...], "row_count": N}
          - 数据>200条：{"success": true, "data": [前20条], "columns": [...], "download_url": "http://...xlsx", "row_count": N, "message": "..."}
        """
        try:
            wrapper, body, err = _read_request_body(self)
            if err:
                self._send_response(*err)
                return

            sql = wrapper.get('sql')
            if not sql:
                self._send_response(400, {'success': False, 'error': '缺少 sql 字段'})
                return

            model_id = wrapper.get('modelId') or wrapper.get('model_id')
            data_source_id = wrapper.get('dataSourceId') or wrapper.get('data_source_id')
            try:
                analysis_contract = normalize_contract(wrapper.get('analysis_contract'))
            except ValueError as exc:
                self._send_response(400, {
                    'success': False, 'code': 'ANALYSIS_CONTRACT_INVALID',
                    'error': str(exc), 'retryable': False,
                })
                return

            if model_id is None:
                self._send_response(400, {
                    'success': False,
                    'code': 'INVALID_REQUEST',
                    'error': '缺少modelId',
                    'retryable': False,
                })
                return
            try:
                model_id = str(_positive_int(model_id, 'modelId'))
                if data_source_id is not None:
                    data_source_id = str(_positive_int(data_source_id, 'dataSourceId'))
            except ValueError as exc:
                self._send_response(400, {
                    'success': False,
                    'code': 'INVALID_REQUEST',
                    'error': str(exc),
                    'retryable': False,
                })
                return

            log(f"[EXECUTE] 执行SQL: {sql[:200]}...")

            # 调用执行器
            translator = get_translator()
            result = translator.execute_sql_only(sql, model_id, data_source_id)

            if result['success']:
                if analysis_contract is not None:
                    # ``row_count`` is the complete SQL result size. For a
                    # download response ``data`` intentionally contains only
                    # the first 20 rows, so normalization must not overwrite
                    # the total with the preview length.
                    total_row_count = result.get('row_count')
                    columns, rows, normalization = normalize_result(
                        analysis_contract,
                        result.get('columns'),
                        result.get('data'),
                    )
                    result['columns'] = columns
                    result['data'] = rows
                    result['row_count'] = (
                        total_row_count
                        if result.get('download_url') and total_row_count is not None
                        else len(rows)
                    )
                    if result.get('download_url'):
                        result['preview_count'] = len(rows)
                        result['preview_truncated'] = result['row_count'] > len(rows)
                    result['analysis_normalization'] = normalization
                    result['analysis_contract'] = validate_result(
                        analysis_contract, columns, rows,
                    )
                    result['analysis_contract']['normalization'] = normalization
                log(f"[EXECUTE] 成功: row_count={result.get('row_count')}")
                if result.get('download_url'):
                    log(f"[EXECUTE] 数据量大，已导出Excel: {result['download_url']}")
                self._send_response(200, result)
            else:
                log(f"[EXECUTE] 失败: error={result.get('error')}")
                self._send_response(200, result)

        except Exception as e:
            import traceback
            log(f"[ERROR] 执行接口异常: {str(e)}\n{traceback.format_exc()}")
            self._send_response(500, {'success': False, 'error': f"服务器内部错误: {str(e)}"})

    def _handle_cache_refresh(self):
        """Refresh in-process DSL caches without restarting the service."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            payload = json.loads(body or '{}')
            model_id = payload.get('modelId')
            translator = get_translator()
            removed = translator.loader.clear_cache(model_id)
            removed['attribute_metadata'] = translator.catalog.clear_cache(model_id)
            log(f"[CACHE] refreshed modelId={model_id}, removed={removed}")
            self._send_response(200, {
                'success': True,
                'modelId': model_id,
                'removed': removed,
            })
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            self._send_response(400, {'success': False, 'error': str(e)})
        except Exception as e:
            log(f"[CACHE] refresh failed: {e}")
            self._send_response(500, {'success': False, 'error': str(e)})

    def _handle_ast_to_sql(self):
        """处理 ASL -> SQL -> 数据查询的完整请求（保持向后兼容）

        入参格式：{"asl": "<ASL的JSON字符串>", "modelId": "<模型ID>"}
        返回格式：{"success": true/false, "result": "<结果JSON字符串>"}
        """
        try:
            wrapper, body, err = _read_request_body(self)
            if err:
                self._send_response(*err)
                return

            asl_str, model_id, err = _extract_asl_and_model_id(wrapper, body)
            if err:
                self._send_response(*err)
                return

            # 检查歧义
            try:
                ast_data = json.loads(asl_str)
            except json.JSONDecodeError as e:
                log(f"[ERROR] ASL解析错误: {str(e)} | 入参: {body}")
                self._send_response(400, {'success': False, 'error': f"ASL解析错误: {str(e)}"})
                return

            ambiguity = ast_data.get('ambiguity', [])
            if ambiguity:
                questions = [{
                    'type': amb.get('type', ''),
                    'question': amb.get('question', ''),
                    'candidates': amb.get('candidates', []),
                } for amb in ambiguity]
                log(f"[ERROR] DSL存在未解决的歧义: {questions} | 入参: {body}")
                self._send_response(400, {
                    'success': False,
                    'error': 'DSL存在未解决的歧义，请先确认后再查询',
                    'ambiguity': questions,
                })
                return

            # 调用翻译器执行查询（翻译 + 数据源关联 + 执行）
            translator = get_translator()
            result = translator.execute_query(asl_str, model_id)

            # 将结果对象序列化为字符串，包装在 result 字段中返回
            result_str = json.dumps(result, ensure_ascii=False)
            wrapper_resp = {'success': result.get('success', False), 'result': result_str}
            if not wrapper_resp['success']:
                wrapper_resp['error'] = result.get('error')

            # 根据成功/失败返回对应状态码
            if wrapper_resp['success']:
                log(f"[RESPONSE] 成功: sql={result.get('sql')}, rows={result.get('row_count')}")
                if result.get('download_url'):
                    log(f"[RESPONSE] 数据导出: {result['download_url']}")
                self._send_response(200, wrapper_resp)
            else:
                # 区分：SQL 生成成功但执行失败（仍返回 SQL 供调试）vs 完全失败
                if result.get('sql'):
                    log(f"[RESPONSE] SQL生成成功但执行失败: sql={result.get('sql')}, error={result.get('error')} | 入参: {body}")
                    self._send_response(200, wrapper_resp)
                else:
                    log(f"[RESPONSE] 失败: error={result.get('error')} | 入参: {body}")
                    self._send_response(500, wrapper_resp)

        except Exception as e:
            import traceback
            log(f"[ERROR] 服务器内部错误: {str(e)}\n{traceback.format_exc()}")
            self._send_response(500, {'success': False, 'error': f"服务器内部错误: {str(e)}"})

    def log_message(self, format, *args):
        pass  # 禁用默认日志


class ProductionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def main():
    log(f"Python解释器: {sys.executable}")
    log(f"当前目录: {os.getcwd()}")
    log(f"脚本目录: {CURRENT_DIR}")
    log("启动API服务（生产版）...")
    # 预热：创建翻译器并测试 Redis 连接
    try:
        t = get_translator()
        t.loader.redis.ping()
        log("Redis 连接正常")
    except Exception as e:
        log(f"Redis 连接失败: {e}")
    host = os.getenv('SQL_TRANSLATOR_API_HOST', '0.0.0.0').strip() or '0.0.0.0'
    try:
        port = int(os.getenv('SQL_TRANSLATOR_API_PORT', '48000'))
    except ValueError as exc:
        raise ValueError('SQL_TRANSLATOR_API_PORT必须是1到65535的整数') from exc
    if not 1 <= port <= 65535:
        raise ValueError('SQL_TRANSLATOR_API_PORT必须是1到65535的整数')
    server = ProductionHTTPServer((host, port), APIHandler)
    log(f"服务地址: http://{host}:{port}")
    log("接口列表:")
    log("  POST /api/translate    - 仅翻译ASL为SQL")
    log("  POST /api/execute      - 执行SQL返回结果")
    log("  POST /api/ast-to-sql   - 完整流程（翻译+执行）")
    log("  POST /api/cache/refresh - 刷新DSL缓存")
    log("  GET  /api/health       - 健康检查")
    log(f"日志文件: {LOG_FILE}")
    server.serve_forever()


if __name__ == '__main__':
    main()
