"""Lossless topic request preparation; editor text alone is not topic evidence."""


def topic_names(tags):
    if not isinstance(tags,str):
        raise ValueError('话题须为文字，请用空格分隔，例如 #日常记录 #剪辑测试。')
    if not tags.strip():return []
    # Keep punctuation within names: never silently split or rewrite a requested topic.
    tokens=tags.split()
    names=[]
    for token in tokens:
        name=token[1:] if token.startswith('#') else token
        if not name or '#' in name or any(ord(c)<32 for c in name):
            raise ValueError('话题格式不正确，请用空格分隔，例如 #日常记录 #剪辑测试。')
        if name not in names:names.append(name)
    return names


def compose_description(body,tags):
    if not isinstance(body,str):raise ValueError('发布简介须为文字。')
    names=topic_names(tags)
    if not names:
        if len(body)>1000:raise ValueError('简介不能超过1000个字符。')
        return body,[]
    suffix=' '.join('#'+name for name in names)
    text=body.rstrip()+ ('\n' if body.strip() and suffix else '')+suffix
    if len(text)>1000:
        raise ValueError('简介与话题合计不能超过1000个字符，请缩短后重试。')
    return text,names
