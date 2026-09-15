from __future__ import annotations
import asyncio
from uuid import UUID
from app.content.raw_text import read_text_lines, numeric_summary
from app.domain.exceptions import AppError
from app.tools.contracts import ToolDescriptor, ToolCallResult, ToolResultStatus


def descriptors():
    attachment = {'type':'string','format':'uuid'}
    shared = dict(provider_type='local',output_schema={'type':'object'},supports_attachments=True,
        supported_attachment_kinds=['text','spreadsheet'],timeout_seconds=60)
    return [ToolDescriptor(tool_id='file.read_text',display_name='原文件文本读取',
        description='直接读取原始TXT/CSV/TSV等文本的指定行或末尾，不受初次预览20万字符限制。',
        input_schema={'type':'object','properties':{'attachment_id':attachment,
            'start_line':{'type':'integer','minimum':1,'default':1},
            'max_lines':{'type':'integer','minimum':1,'maximum':1000,'default':200},
            'tail':{'type':'boolean','default':False}},'required':['attachment_id'],'additionalProperties':False},**shared),
        ToolDescriptor(tool_id='file.numeric_summary',display_name='文本数值列统计',
        description='完整扫描文本数据中明确指定的数值列，计算样本数、均值、极值、有效值和标准差。列、单位、采样率须来自用户或文件，不能猜测。',
        input_schema={'type':'object','properties':{'attachment_id':attachment,
            'value_column':{'type':'integer','minimum':1,'maximum':1000,'description':'从1开始的数值列位置'},
            'delimiter':{'type':'string','enum':['auto','whitespace',',','\t',';','|'],'default':'auto'},
            'header_lines':{'type':'integer','minimum':0,'maximum':1000,'default':0},
            'sample_rate_hz':{'type':['number','null'],'exclusiveMinimum':0},
            'unit':{'type':['string','null'],'maxLength':50}},
            'required':['attachment_id','value_column'],'additionalProperties':False},**shared)]


class RawFileTools:
    def __init__(self, attachment_service):
        self.attachments = attachment_service

    async def execute(self, request, data_access_token=None):
        desc, data = await self.attachments.read_owned(attachment_id=UUID(request.arguments['attachment_id']),user_token=request.user_token)
        if desc.kind.value != 'text' and not desc.filename.lower().endswith(('.csv','.tsv')):
            raise AppError('TEXT_FILE_REQUIRED','该工具需要TXT、CSV、TSV等文本数据；Excel请使用表格工具。',422)
        args = dict(request.arguments)
        args.pop('attachment_id',None)
        fn = read_text_lines if request.tool_id == 'file.read_text' else numeric_summary
        result = await asyncio.to_thread(fn,data,**args)
        result.update(filename=desc.filename,attachment_id=str(desc.attachment_id))
        return ToolCallResult(tool_id=request.tool_id,status=ToolResultStatus.SUCCESS,
            content=result,structured_content=result,metadata={'deterministic':True})
