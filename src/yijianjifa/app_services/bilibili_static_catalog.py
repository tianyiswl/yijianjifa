"""Reviewed official public B站 names, not account permission or posting IDs.

New human choices intentionally have no numeric platform ID. Resolve them only
against the original posting page's normal responses immediately before use.
No network, browser, credential, or database access occurs in this module.
"""
import copy

VERSION = 'bilibili-public-20261009-current-only'
DEFAULT_FIELDS = {'category_choice': 'human:knowledge', 'creation_statement_choice': 'no_mark'}
SOURCES = (
    'https://m.bilibili.com/',
    'https://s1.hdslb.com/bfs/seed/jinkela/header-v2/header.js',
    'https://s1.hdslb.com/bfs/static/creator-monorepo/videoup/static/js/index.02c1a54b.js',
    'https://www.bilibili.com/opus/1106496554576904197',
    'https://www.bilibili.com/opus/1162070497975861281',
)
# Official main-site channel route and name only; tid is not human_type2.
HUMAN_CATEGORIES = (
    ('human:cinephile', '影视'),
    ('human:ent', '娱乐'),
    ('human:music', '音乐'),
    ('human:dance', '舞蹈'),
    ('human:douga', '动画'),
    ('human:painting', '绘画'),
    ('human:kichiku', '鬼畜'),
    ('human:game', '游戏'),
    ('human:information', '资讯'),
    ('human:knowledge', '知识'),
    ('human:ai', '人工智能'),
    ('human:tech', '科技数码'),
    ('human:car', '汽车'),
    ('human:fashion', '时尚美妆'),
    ('human:home', '家装房产'),
    ('human:outdoors', '户外潮流'),
    ('human:gym', '健身'),
    ('human:sports', '体育运动'),
    ('human:handmake', '手工'),
    ('human:food', '美食'),
    ('human:shortplay', '小剧场'),
    ('human:travel', '旅游出行'),
    ('human:rural', '三农'),
    ('human:animal', '动物'),
    ('human:parenting', '亲子'),
    ('human:health', '健康'),
    ('human:emotion', '情感'),
    ('human:mysticism', '神秘学'),
    ('human:vlog', 'vlog'),
    ('human:life_joy', '生活兴趣'),
    ('human:life_experience', '生活经验'),
 )
# The official legacy navigation publishes these old tid/name/parent tuples.
# Keep their identities for saved records; they are not new UI choices.
LEGACY_CATEGORIES = (
    (24, 'MAD·AMV', '动画'),
    (25, 'MMD·3D', '动画'),
    (47, '短片·手书', '动画'),
    (257, '配音', '动画'),
    (210, '手办·模玩', '动画'),
    (86, '特摄', '动画'),
    (253, '动漫杂谈', '动画'),
    (27, '综合', '动画'),
    (33, '连载动画', '番剧'),
    (32, '完结动画', '番剧'),
    (51, '资讯', '番剧'),
    (152, '官方延伸', '番剧'),
    (153, '国产动画', '国创'),
    (168, '国产原创相关', '国创'),
    (169, '布袋戏', '国创'),
    (195, '动态漫·广播剧', '国创'),
    (170, '资讯', '国创'),
    (28, '原创音乐', '音乐'),
    (31, '翻唱', '音乐'),
    (59, '演奏', '音乐'),
    (30, 'VOCALOID·UTAU', '音乐'),
    (29, '音乐现场', '音乐'),
    (193, 'MV', '音乐'),
    (243, '乐评盘点', '音乐'),
    (244, '音乐教学', '音乐'),
    (130, '音乐综合', '音乐'),
    (20, '宅舞', '舞蹈'),
    (198, '街舞', '舞蹈'),
    (199, '明星舞蹈', '舞蹈'),
    (200, '国风舞蹈', '舞蹈'),
    (255, '手势·网红舞', '舞蹈'),
    (154, '舞蹈综合', '舞蹈'),
    (156, '舞蹈教程', '舞蹈'),
    (17, '单机游戏', '游戏'),
    (171, '电子竞技', '游戏'),
    (172, '手机游戏', '游戏'),
    (65, '网络游戏', '游戏'),
    (173, '桌游棋牌', '游戏'),
    (121, 'GMV', '游戏'),
    (136, '音游', '游戏'),
    (19, 'Mugen', '游戏'),
    (201, '科学科普', '知识'),
    (124, '社科·法律·心理', '知识'),
    (228, '人文历史', '知识'),
    (207, '财经商业', '知识'),
    (208, '校园学习', '知识'),
    (209, '职业职场', '知识'),
    (229, '设计·创意', '知识'),
    (122, '野生技能协会', '知识'),
    (95, '数码', '科技'),
    (230, '软件应用', '科技'),
    (231, '计算机技术', '科技'),
    (232, '科工机械', '科技'),
    (233, '极客DIY', '科技'),
    (235, '篮球', '运动'),
    (249, '足球', '运动'),
    (164, '健身', '运动'),
    (236, '竞技体育', '运动'),
    (237, '运动文化', '运动'),
    (238, '运动综合', '运动'),
    (245, '赛车', '汽车'),
    (246, '改装玩车', '汽车'),
    (247, '新能源车', '汽车'),
    (248, '房车', '汽车'),
    (240, '摩托车', '汽车'),
    (227, '购车攻略', '汽车'),
    (176, '汽车生活', '汽车'),
    (138, '搞笑', '生活'),
    (254, '亲子', '生活'),
    (250, '出行', '生活'),
    (251, '三农', '生活'),
    (239, '家居房产', '生活'),
    (161, '手工', '生活'),
    (162, '绘画', '生活'),
    (21, '日常', '生活'),
    (76, '美食制作', '美食'),
    (212, '美食侦探', '美食'),
    (213, '美食测评', '美食'),
    (214, '田园美食', '美食'),
    (215, '美食记录', '美食'),
    (218, '喵星人', '动物圈'),
    (219, '汪星人', '动物圈'),
    (222, '小宠异宠', '动物圈'),
    (221, '野生动物', '动物圈'),
    (220, '动物二创', '动物圈'),
    (75, '动物综合', '动物圈'),
    (22, '鬼畜调教', '鬼畜'),
    (26, '音MAD', '鬼畜'),
    (126, '人力VOCALOID', '鬼畜'),
    (216, '鬼畜剧场', '鬼畜'),
    (127, '教程演示', '鬼畜'),
    (157, '美妆护肤', '时尚'),
    (252, '仿妆cos', '时尚'),
    (158, '穿搭', '时尚'),
    (159, '时尚潮流', '时尚'),
    (203, '热点', '资讯'),
    (204, '环球', '资讯'),
    (205, '社会', '资讯'),
    (206, '综合', '资讯'),
    (71, '综艺', '娱乐'),
    (241, '娱乐杂谈', '娱乐'),
    (242, '粉丝创作', '娱乐'),
    (137, '明星综合', '娱乐'),
    (182, '影视杂谈', '影视'),
    (183, '影视剪辑', '影视'),
    (85, '小剧场', '影视'),
    (256, '短片', '影视'),
    (184, '预告·资讯', '影视'),
    (37, '人文·历史', '纪录片'),
    (178, '科学·探索·自然', '纪录片'),
    (179, '军事', '纪录片'),
    (180, '社会·美食·旅行', '纪录片'),
    (147, '华语电影', '电影'),
    (145, '欧美电影', '电影'),
    (146, '日本电影', '电影'),
    (83, '其他国家', '电影'),
    (185, '国产剧', '电视剧'),
    (187, '海外剧', '电视剧'),
 )
STATEMENT_CHOICES = {
    'no_mark': {'name': '不添加声明', 'platform_id': -1},
    'reprint': {'name': '转载', 'platform_id': -2},
    # Match only exact labels observed on the ordinary posting page. The ID
    # still comes from that page; multiple matching aliases remain ambiguous.
    'ai_generated': {'name': '该视频使用人工智能合成技术', 'platform_names': ('该视频使用人工智能合成技术', '该内容使用人工智能合成技术', '含AI生成内容')},
    'fiction_entertainment': {'name': '该内容仅供娱乐，请勿过分解读', 'platform_names': ('该内容仅供娱乐，请勿过分解读',)},
}


def category_choices():
    return [{'choice': key, 'name': name, 'mode': 'human'} for key, name in HUMAN_CATEGORIES]


def _category_record(value):
    if type(value) is not str:return None
    for choice,name in HUMAN_CATEGORIES:
        if value==choice:return {'choice':choice,'name':name,'mode':'human'}
    for tid,name,parent in LEGACY_CATEGORIES:
        if value=='legacy:'+str(tid):return {'choice':value,'name':name,'parent_name':parent,'mode':'legacy'}
    return None


def with_defaults(fields):
    """Default fresh intentions, preserving saved semantic and concrete values."""
    result=copy.deepcopy(fields)
    for key,alternatives in (
        ('category_choice',('category_choice','category_id','category_name','category_mode')),
        ('creation_statement_choice',('creation_statement_choice','creation_statement_id','creation_statement_name')),
    ):
        if not any(name in result for name in alternatives):result[key]=DEFAULT_FIELDS[key]
    return result


def builtin_options():
    return {'status': 'ready', 'source': 'builtin', 'catalog_version': VERSION,
        'category_choices': category_choices(),
        'creation_statement_choices': [{'choice': key, 'name': value['name']} for key, value in STATEMENT_CHOICES.items()],
        'categories': [], 'statements': []}


def valid_category_choice(value):
    return _category_record(value) is not None


def valid_statement_choice(value):
    return type(value) is str and value in STATEMENT_CHOICES


def _one(rows, predicate, message):
    matches=[row for row in rows if type(row) is dict and predicate(row)] if type(rows) is list else []
    if len(matches)!=1: raise ValueError(message)
    return matches[0]


def resolve_fields(fields, options=None, *, local=False):
    """Resolve immutable semantic intent only using unique observed choices.

    Local validation deliberately leaves category_id unresolved. Local product
    labels for no_mark/reprint are never treated as official form labels.
    """
    if type(fields) is not dict: raise ValueError('B站发布字段无效')
    result=copy.deepcopy(fields)
    observed=options if type(options) is dict else {}
    if 'category_choice' in fields:
        choice=fields['category_choice']
        if not valid_category_choice(choice): raise ValueError('请选择有效的B站分区')
        if any(key in fields for key in ('category_id','category_name','category_mode')):
            raise ValueError('B站分区意图与具体分区字段冲突')
        row=_category_record(choice)
        if local:
            result.update(category_id=None,category_name=row['name'],category_mode=row['mode'])
        else:
            match=_one(observed.get('categories'),lambda item:
                type(item.get('id')) is int and item['id']>0 and item.get('name')==row['name'] and item.get('mode')==row['mode']
                and (row['mode']!='legacy' or (item['id']==int(choice.split(':')[1]) and item.get('parent_name')==row['parent_name'])),
                '所选B站分区在当前投稿表单中不存在或不唯一，请修改原目标分区')
            result.update(category_id=match['id'],category_name=match['name'],category_mode=match['mode'])
    if 'creation_statement_choice' in fields:
        choice=fields['creation_statement_choice']
        if not valid_statement_choice(choice): raise ValueError('请选择有效的B站创作声明')
        if any(key in fields for key in ('creation_statement_id','creation_statement_name')):
            raise ValueError('B站声明意图与具体声明字段冲突')
        intent=STATEMENT_CHOICES[choice]
        if local:
            result.update(creation_statement_id=intent.get('platform_id'),creation_statement_name=intent['name'])
        else:
            match=_one(observed.get('statements'),lambda item:
                type(item.get('id')) is int and (item['id']==intent['platform_id'] if 'platform_id' in intent else item['id']>0 and item.get('name') in intent['platform_names'])
                and type(item.get('name')) is str and 1<=len(item['name'])<=255,
                '所选B站创作声明在当前投稿表单中不存在或不唯一，请修改原目标声明')
            result.update(creation_statement_id=match['id'],creation_statement_name=match['name'])
    return result
