from copy import deepcopy
from unittest.mock import Mock

import pytest
import api_server_prod as api
from test_single_domain_execution import request_api
from test_analysis_contract import CONTRACT


@pytest.mark.parametrize('export_fails', [False, True])
def test_normalized_complete_result_and_attachment_agree(monkeypatch, export_fails):
    rows = [{'paid_at': f'2026-{month:02}-01', 'unit_price': 10 + i, 'quantity': i + 1}
            for month in (7, 8) for i in range(21)]
    original = dict(success=True, columns=list(rows[0]), data=rows, row_count=len(rows),
                    download_url='https://files.example/raw.xlsx', preview_count=len(rows), preview_truncated=False)
    translator=Mock();translator.execute_sql_only.return_value=deepcopy(original)
    monkeypatch.setattr(api, '_request_translator', lambda _:translator)
    monkeypatch.setattr(api,'log',lambda _:None)
    exporter=Mock();exporter.export_to_excel.return_value='https://files.example/processed.xlsx'
    if export_fails: exporter.export_to_excel.side_effect=RuntimeError('private-error')
    monkeypatch.setattr('data_exporter.get_exporter',lambda:exporter)
    status,result=request_api('_handle_execute',dict(sql='SELECT price FROM facts', modelId=81, analysis_contract=CONTRACT))
    assert status==200 and result['success'] and result['row_count']==2
    assert result['analysis_normalization']['source_row_count']==42
    assert result['analysis_contract']['contract_satisfied']
    exporter.export_to_excel.assert_called_once_with(result['columns'],result['data'])
    assert not result['preview_truncated'] and result['preview_count']==2
    assert result['download_url']==(None if export_fails else 'https://files.example/processed.xlsx')
    assert 'raw.xlsx' not in str(result) and 'private-error' not in str(result)


def test_partial_legacy_response_cannot_be_normalized_into_a_fake_full_result(monkeypatch):
    rows=[{'paid_at':'2026-07-01','unit_price':10,'quantity':2},
          {'paid_at':'2026-08-01','unit_price':20,'quantity':3}]
    translator=Mock();translator.execute_sql_only.return_value=dict(success=True, columns=list(rows[0]),
        data=rows,row_count=2154,preview_truncated=True,download_url=None)
    monkeypatch.setattr(api,'_request_translator',lambda _:translator)
    monkeypatch.setattr(api,'log',lambda _:None)
    _,result=request_api('_handle_execute',dict(sql='SELECT price FROM facts',modelId=81,analysis_contract=CONTRACT))
    assert result['row_count']==2154 and result['data']==rows and result['preview_truncated']
    assert result['analysis_normalization']['reason']=='incomplete_source_result'
