"""Full-source text retrieval and explicit-column numeric analysis, off the event loop."""
from __future__ import annotations
import csv
import heapq
import io
import math
import re
from app.content.parsers.text import decode_text


def search_text(data, tokens, max_characters, chunk_size=1500, overlap=200):
    text, encoding = decode_text(data)
    step = max(1, chunk_size-overlap)
    def candidates():
        for index, offset in enumerate(range(0,len(text),step),1):
            part = text[offset:offset+chunk_size]
            low = part.lower()
            yield (sum(3 for token in tokens if token in low), -index, offset, part)
    # Retain only the number of chunks that can fit in the model context.
    selected = heapq.nlargest(max(1, math.ceil(max_characters/max(1,chunk_size))), candidates())
    matches, used = [], 0
    for score, index, offset, part in selected:
        part = part[:max(0,max_characters-used)]
        if not part:
            continue
        start_line = text.count('\n',0,offset)+1
        matches.append({'sequence_no':-index,'score':score,'content':part,
            'locator':{'character_start':offset,'line_start':start_line,'line_end':start_line+part.count('\n')}})
        used += len(part)
    return {'matches':matches,'match_count':len(matches),'source_characters':len(text),
        'source_line_count':len(text.splitlines()), 'source_scanned_complete':True,
        'context_limited':used<len(text),'encoding':encoding,
        'warnings':['已检索原文件全文，本轮只向模型提供相关片段。'] if used<len(text) else []}


def read_text_lines(data, start_line=1, max_lines=200, tail=False, max_characters=30000):
    text, encoding = decode_text(data)
    lines = text.splitlines()
    start = max(0,len(lines)-max_lines) if tail else max(0,start_line-1)
    selected = lines[start:start+max_lines]
    raw = '\n'.join(selected)
    output = raw[:max_characters]
    return {'content':output,'line_start':start+1,'line_end':start+output.count('\n')+1 if output else start,
        'source_line_count':len(lines),'characters_limited':len(output)<len(raw),'encoding':encoding}


def numeric_summary(data, value_column, delimiter='auto', header_lines=0, sample_rate_hz=None, unit=None):
    text, encoding = decode_text(data)
    if delimiter == 'auto':
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',\t;|').delimiter
        except csv.Error:
            delimiter = 'whitespace'
    rows = (line.split() for line in io.StringIO(text)) if delimiter == 'whitespace' else csv.reader(io.StringIO(text), delimiter=delimiter)
    n, skipped, source_rows = 0, 0, 0
    mean, m2, square_mean = 0.0, 0.0, 0.0
    minimum, maximum, first = None, None, []
    for line, values in enumerate(rows,1):
        source_rows = line
        if line <= header_lines:
            continue
        try:
            value = float(values[value_column-1])
            if not math.isfinite(value) or abs(value)>1e150:
                raise ValueError()
        except (ValueError, IndexError):
            skipped += 1
            continue
        n += 1
        delta = value-mean
        mean += delta/n
        m2 += delta*(value-mean)
        square_mean += (value*value-square_mean)/n
        minimum = value if minimum is None else min(minimum,value)
        maximum = value if maximum is None else max(maximum,value)
        if len(first)<10:
            first.append({'line':line,'value':value})
    return {'sample_count':n,'value_column':value_column,'source_rows':source_rows,
        'skipped_non_numeric_rows':skipped, 'header_lines':header_lines, 'source_scanned_complete':True,
        'mean':mean if n else None,'minimum':minimum,'maximum':maximum,
        'peak_to_peak':maximum-minimum if n else None,
        'rms':math.sqrt(max(0,square_mean)) if n else None,
        'population_stddev':math.sqrt(max(0,m2/n)) if n else None,
        'sample_rate_hz':sample_rate_hz, 'duration_seconds':(n-1)/sample_rate_hz if sample_rate_hz and n and not skipped else None,
        'unit':unit, 'encoding':encoding,'first_samples':first,
        'limitations':['仅对指定列进行数值统计；没有执行专业故障诊断或频谱计算。'] +
            (['部分行不是有效数值，统计结果仅覆盖有效数值行。'] if skipped else [])}
