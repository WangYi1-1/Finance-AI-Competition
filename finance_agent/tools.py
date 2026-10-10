"""实际流水线工具目录：供执行记录及外部适配器发现契约，不宣称为MCP服务。"""
from pydantic import TypeAdapter
from .schemas import AnalysisResult, ExtractedReport, ParsedDocument, RunResult, Record


class EnrichmentInput(Record):
    report: ExtractedReport
    document: ParsedDocument


def tool_catalog():
    return [
        {"name": "parse_financial_doc", "input_schema": {"type": "object", "properties": {"pdf": {"type": "string"}}, "required": ["pdf"], "additionalProperties": False}, "output_schema": ParsedDocument.model_json_schema()},
        {"name": "extract_metrics", "input_schema": ParsedDocument.model_json_schema(), "output_schema": ExtractedReport.model_json_schema()},
        {"name": "enrich_financial_details", "input_schema": EnrichmentInput.model_json_schema(), "output_schema": ExtractedReport.model_json_schema()},
        {"name": "financial_analysis", "input_schema": TypeAdapter(list[ExtractedReport]).json_schema(), "output_schema": AnalysisResult.model_json_schema()},
        {"name": "generate_report", "input_schema": RunResult.model_json_schema(), "output_schema": {"type": "object", "properties": {"files": {"type": "array", "items": {"type": "string"}}}, "required": ["files"]}},
    ]
