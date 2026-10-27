"""Short creator-facing reasons; never retain raw responses or credentials."""
import re


def safe_message(value):
    if type(value) is not str:
        return ''
    text = re.sub(r'\s+', ' ', value[:2000]).strip()
    text = re.sub(r'(?i)(?:https?://|www\.)\S+', '[链接]', text)
    text = re.sub(r'(?i)(?:cookie|authorization|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=：]\s*\S+', '[私密信息]', text)
    text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机号]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[邮箱]', text)
    text = re.sub(r'[\x00-\x1f\x7f<>]', '', text)
    return text[:200]


def failure_message(evidence, state):
    if state not in {'platform_rejected', 'pre_submit_failed', 'result_unknown', 'interrupted'}:
        return ''
    if state=='pre_submit_failed' and evidence.get('error_code')=='posting_limit_skipped':
        from .posting_limits import SKIP_REASON
        return SKIP_REASON
    text = safe_message(evidence.get('platform_message'))
    if not text:
        return ''
    if state in {'result_unknown', 'interrupted'}:
        return text + '；提交结果尚未确认，请勿重复发布。'
    return text
