"""All model-catalog execution enters here; IDs select code, never question text."""
from .registry import registered_operator


def execute(engine,request,columns,rows,knowledge):
    from app.analysis.engine import AnalysisError
    from app.domain.models import PrimaryIntent as I
    from app.analysis.independent_tools import trend
    selection=request._analysis_contract['tool_selection']
    selected=set(selection['capabilities'])
    special=registered_operator(selection)
    if special is not None:
        output=engine._composable_analysis(request,columns,rows)
        if output is None:raise AnalysisError('REGISTERED_ANALYSIS_OPERATOR_UNAVAILABLE')
        return output
    handlers={I.TREND_ANALYSIS:lambda:trend(engine,request,columns,rows,selected),
        I.COMPARISON_ANALYSIS:lambda:engine._comparison(request,columns,rows,knowledge),
        I.COMPOSITION_ANALYSIS:lambda:engine._composition(request,columns,rows,knowledge),
        I.ANOMALY_ANALYSIS:lambda:engine._anomaly(request,columns,rows,knowledge),
        I.ROOT_CAUSE_ANALYSIS:lambda:engine._root_cause(request,columns,rows,knowledge),
        I.FORECAST_ANALYSIS:lambda:engine._forecast(request,columns,rows,knowledge)}
    if request.primary_intent not in handlers:raise AnalysisError('UNREGISTERED_ANALYSIS_CHAIN')
    return handlers[request.primary_intent]()
