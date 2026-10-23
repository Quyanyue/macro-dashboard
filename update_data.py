#!/usr/bin/env python3
"""宏观策略看板 · 一键更新数据

从官方来源抓取宏观数据，生成网站读取的 data.js：
  - 国家统计局「最新发布」：CPI、PPI、PMI、工业增加值、社零、固投、地产、工业利润、调查失业率
  - 中国人民银行「金融统计数据报告」：M2、M1、社融存量、人民币贷款
  - OECD 关键经济指标库（官方 SDMX 接口）：GDP、出口进口、国债利率、汇率，以及美国 / 欧元区 / 日本指标

每个数据点都记录出处网址。已抓过的发布稿缓存在 cache/ 里，再次运行只抓新增的稿子。

用法：
    python3 update_data.py            # 增量更新（只看统计局、央行列表前几页）
    python3 update_data.py --full     # 全量重扫所有列表页
    python3 update_data.py --build    # 只改了观点 / 事件 / 网页时，不联网直接重新打包
运行后把整个文件夹重新上传到网站即可。
"""
import csv, hashlib, html, io, json, os, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, 'cache')
CONTENT = os.path.join(ROOT, 'content')
FULL = '--full' in sys.argv
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
os.makedirs(CACHE, exist_ok=True)

# 国内政府网站直连，境外接口走系统代理（如有）
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor())
_proxied = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


STATUS = {}   # 各来源本次抓取情况：{来源: {'ok': n, 'fail': n}}


def _source(url):
    return ('国家统计局' if 'stats.gov.cn' in url else '中国人民银行' if 'pbc.gov.cn' in url else 'OECD' if 'oecd.org' in url
            else '东方财富研报' if 'eastmoney.com' in url else '新浪研报' if 'sina.com.cn' in url else '其他')


def _mark(url, ok):
    s = STATUS.setdefault(_source(url), {'ok': 0, 'fail': 0})
    s['ok' if ok else 'fail'] += 1


def fetch(url, cache=True, timeout=30):
    key = os.path.join(CACHE, hashlib.md5(url.encode()).hexdigest() + '.html')
    if cache and os.path.exists(key):
        return open(key, encoding='utf-8').read()
    opener = _direct if ('.gov.cn' in url or 'eastmoney.com' in url) else _proxied
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'})
            raw = opener.open(req, timeout=timeout).read()
            text = raw.decode('utf-8', errors='ignore')
            if cache and len(text) > 3000:      # 过短多半是拦截页，不缓存
                open(key, 'w', encoding='utf-8').write(text)
            _mark(url, len(text) > 3000)
            time.sleep(0.3)
            return text
        except Exception as e:  # 网络抖动重试
            err = e
            time.sleep(2 * (attempt + 1))
    _mark(url, False)
    print('  ! 抓取失败', url, err)
    return ''


def plain(t):
    t = re.sub(r'<script.*?</script>|<style.*?</style>', '', t, flags=re.S)
    t = html.unescape(re.sub(r'<[^>]+>', ' ', t))
    t = t.replace('­', '').replace('，', ',').replace('（', '(').replace('）', ')').replace('：', ':').replace('－', '-')
    t = re.sub(r'\s+', '', t)          # 统计局正文的数字与百分号之间有空格，统一去掉
    return t.replace('—', '-').replace('–', '-')


def signed(word, num):
    v = float(num)
    return -v if word in ('下降', '回落', '减少') else v


OBS = {}   # (sid, date) -> {value, src}


def put(sid, date, value, src):
    OBS[(sid, date)] = {'sid': sid, 'date': date, 'value': round(value, 4), 'src': src}


# ---------------------------------------------------------------- 国家统计局
def nbs_list():
    pages = ['https://www.stats.gov.cn/sj/zxfb/']
    first = fetch(pages[0], cache=False)
    m = re.search(r'createPageHTML\((\d+),', first)
    n = int(m.group(1)) if m else 1
    limit = n if FULL or not os.path.exists(os.path.join(CACHE, 'nbs_index.json')) else min(n, 4)
    pages += ['https://www.stats.gov.cn/sj/zxfb/index_%d.html' % i for i in range(1, limit)]
    idx_path = os.path.join(CACHE, 'nbs_index.json')
    items = json.load(open(idx_path, encoding='utf-8')) if os.path.exists(idx_path) else {}
    for i, p in enumerate(pages):
        t = first if i == 0 else fetch(p, cache=False)
        for href, txt in re.findall(r'<a[^>]+href="(\./\d{6}/t\d+_\d+\.html)"[^>]*>(.*?)</a>', t, re.S):
            txt = re.sub(r'<[^>]+>|\s+', ' ', txt).strip()
            if not txt.endswith('...'):
                items['https://www.stats.gov.cn/sj/zxfb/' + href[2:]] = txt
    json.dump(items, open(idx_path, 'w', encoding='utf-8'), ensure_ascii=False)
    return items


def period_from(title, url):
    m = re.search(r'(\d{4})年(?:1-|1—)?(\d{1,2})月', title.replace('—', '-'))
    if m:
        return '%s-%02d' % (m.group(1), int(m.group(2)))
    d = re.search(r'/t(\d{4})(\d{2})\d{2}_', url)            # 标题无年份时，用发布月的上一个月
    y, mo = int(d.group(1)), int(d.group(2)) - 1
    if mo == 0:
        y, mo = y - 1, 12
    mm = re.search(r'(\d{1,2})月份', title)
    if mm:
        mo2 = int(mm.group(1))
        if mo2 > mo:
            y -= 1
        mo = mo2
    return '%d-%02d' % (y, mo)


NUM = r'(-?\d+(?:\.\d+)?)'


def prefetch(urls):
    todo = [u for u in urls if not os.path.exists(os.path.join(CACHE, hashlib.md5(u.encode()).hexdigest() + '.html'))]
    if todo:
        print('  新抓取 %d 篇发布稿…' % len(todo), flush=True)
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(fetch, todo))


NBS_KINDS = ('居民消费价格', '工业生产者出厂价格', '采购经理指数运行情况', '规模以上工业增加值', '社会消费品零售总额',
             '全国固定资产投资', '房地产', '工业企业利润', '国民经济', '经济运行')


def parse_nbs(items):
    prefetch([u for u, t in items.items() if any(k in t for k in NBS_KINDS) and '解读' not in t and '答记者问' not in t])
    for url, title in sorted(items.items()):
        kind = None
        for k in ('居民消费价格', '工业生产者出厂价格', '采购经理指数运行情况', '规模以上工业增加值', '社会消费品零售总额',
                  '全国固定资产投资', '房地产', '工业企业利润', '国民经济', '经济运行'):
            if k in title:
                kind = k
                break
        if not kind or '解读' in title or '答记者问' in title:
            continue
        t = plain(fetch(url))
        if not t:
            continue
        per = period_from(title, url)
        if kind == '居民消费价格':
            m = re.search(r'全国居民消费价格同比(上涨|下降)' + NUM + '%', t) or re.search(r'全国居民消费价格同比(持平)()', t)
            if m: put('cn_cpi', per, signed(m.group(1), m.group(2) or 0), url)
            m = re.search(r'扣除食品和能源价格的核心CPI同比(上涨|下降)' + NUM + '%', t)
            if m: put('cn_core_cpi', per, signed(m.group(1), m.group(2)), url)
            m = re.search(r'全国居民消费价格环比(上涨|下降)' + NUM + '%', t)
            if m: put('cn_cpi_mom', per, signed(m.group(1), m.group(2)), url)
        elif kind == '工业生产者出厂价格':
            m = re.search(r'工业生产者出厂价格同比(上涨|下降)' + NUM + '%', t)
            if m: put('cn_ppi', per, signed(m.group(1), m.group(2)), url)
            m = re.search(r'工业生产者购进价格同比(上涨|下降)' + NUM + '%', t)
            if m: put('cn_ppirm', per, signed(m.group(1), m.group(2)), url)
        elif kind == '采购经理指数运行情况':
            m = re.search(r'制造业采购经理指数\(PMI\)为' + NUM + '%', t)
            if m: put('cn_pmi', per, float(m.group(1)), url)
            m = re.search(r'非制造业商务活动指数为' + NUM + '%', t)
            if m: put('cn_npmi', per, float(m.group(1)), url)
            m = re.search(r'综合PMI产出指数为' + NUM + '%', t)
            if m: put('cn_cpmi', per, float(m.group(1)), url)
            m = re.search(r'新订单指数为' + NUM + '%', t)
            if m: put('cn_pmi_no', per, float(m.group(1)), url)
        elif kind == '规模以上工业增加值':
            m = re.search(r'(?<![-\d])\d{1,2}月份,规模以上工业增加值同比实际(增长|下降)' + NUM + '%', t)
            if m: put('cn_ip', per, signed(m.group(1), m.group(2)), url)
            elif re.search(r'1-2月份', title.replace('—', '-')):
                m = re.search(r'1-2月份,规模以上工业增加值同比实际(增长|下降)' + NUM + '%', t)
                if m: put('cn_ip', per, signed(m.group(1), m.group(2)), url)
        elif kind == '社会消费品零售总额':
            m = re.search(r'(?<![-\d])\d{1,2}月份,社会消费品零售总额\d+亿元,同比(增长|下降)' + NUM + '%', t)
            if m: put('cn_retail', per, signed(m.group(1), m.group(2)), url)
            m = re.search(r'1-\d{1,2}月份,社会消费品零售总额\d+亿元,同比(增长|下降)' + NUM + '%', t)
            if m: put('cn_retail_ytd', per, signed(m.group(1), m.group(2)), url)
        elif kind == '全国固定资产投资':
            m = re.search(r'全国固定资产投资\(不含农户\)\d+亿元,(?:同比|比上年)(增长|下降)' + NUM + '%', t)
            if m: put('cn_fai', per, signed(m.group(1), m.group(2)), url)
            m = re.search(r'制造业投资(?:同比|比上年)?(增长|下降)' + NUM + '%', t)
            if m: put('cn_fai_mfg', per, signed(m.group(1), m.group(2)), url)
            m = re.search(r'基础设施投资(?:\(不含电力[^)]*\))?(?:\(口径详见附注\d\))?(?:同比|比上年)(增长|下降)' + NUM + '%', t)
            if m: put('cn_fai_infra', per, signed(m.group(1), m.group(2)), url)
        elif kind == '房地产':
            m = re.search(r'全国房地产开发投资\d+亿元,(?:同比|比上年)(增长|下降)' + NUM + '%', t)
            if m: put('cn_re_inv', per, signed(m.group(1), m.group(2)), url)
            m = re.search(r'新建商品房销售面积\d+万平方米,(?:同比|比上年)(增长|下降)' + NUM + '%', t)
            if m: put('cn_re_sales', per, signed(m.group(1), m.group(2)), url)
        elif kind == '工业企业利润':
            m = re.search(r'全国规模以上工业企业(?:实现)?利润总额\d+(?:\.\d+)?亿元,(?:同比|比上年)(增长|下降)' + NUM + '%', t)
            if m: put('cn_profit', per, signed(m.group(1), m.group(2)), url)
        elif kind in ('国民经济', '经济运行'):
            m = re.search(r'(?<![-\d])(\d{1,2})月份,全国城镇调查失业率为' + NUM + '%', t)
            if m: put('cn_ur', per, float(m.group(2)), url)


# ---------------------------------------------------------------- 人民银行
def pbc_period(title):
    """报告标题 → 期末月份：'2026年8月' → 08，'一季度' → 03，'上半年' → 06，'前三季度' → 09，'2025年金融统计…' → 12"""
    y = re.search(r'(\d{4})年', title)
    if not y:
        return None
    m = re.search(r'\d{4}年(\d{1,2})月', title)
    if m:
        mo = int(m.group(1))
    elif '一季度' in title:
        mo = 3
    elif '上半年' in title:
        mo = 6
    elif '三季度' in title:
        mo = 9
    elif re.search(r'\d{4}年(金融统计|社会融资规模存量)', title):
        mo = 12
    else:
        return None
    return '%s-%02d' % (y.group(1), mo)


def pbc():
    base = 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/'
    fetch('https://www.pbc.gov.cn/', cache=False)          # 先拿 cookie
    first = fetch(base + 'index.html', cache=False)
    m = re.search(r'totalpage="(\d+)"', first)
    n = int(m.group(1)) if m else 1
    idx_path = os.path.join(CACHE, 'pbc_index.json')
    items = json.load(open(idx_path, encoding='utf-8')) if os.path.exists(idx_path) else {}
    limit = n if FULL or not items else min(n, 3)
    limit = min(limit, 24)                                  # 约到 2016 年
    for i in range(1, limit + 1):
        t = first if i == 1 else fetch(base + '11871-%d.html' % i, cache=False)
        for h, x in re.findall(r'<a[^>]+href="(/diaochatongjisi/116219/116225/[^"]+/index\.html)"[^>]*>(.*?)</a>', t, re.S):
            x = re.sub(r'<[^>]+>|\s+', '', x)
            if '金融统计数据报告' in x or '社会融资规模存量统计' in x:
                items['https://www.pbc.gov.cn' + h] = x
    json.dump(items, open(idx_path, 'w', encoding='utf-8'), ensure_ascii=False)
    prefetch(list(items))
    for url, title in sorted(items.items()):
        t = plain(fetch(url))
        if not t:
            continue
        per = pbc_period(title)
        if not per:
            m = re.search(r'(\d{4})年(\d{1,2})月末', t)
            if not m:
                continue
            per = '%s-%02d' % (m.group(1), int(m.group(2)))
        a = re.search(r'广义货币\(M2\)余额[\d.]+万亿元,同比(增长|下降)' + NUM + '%', t)
        if a: put('cn_m2', per, signed(a.group(1), a.group(2)), url)
        a = re.search(r'狭义货币\(M1\)余额[\d.]+万亿元,同比(增长|下降)' + NUM + '%', t)
        if a: put('cn_m1', per, signed(a.group(1), a.group(2)), url)
        a = re.search(r'社会融资规模存量为[\d.]+万亿元,同比(增长|下降)' + NUM + '%', t)
        if a: put('cn_tsf', per, signed(a.group(1), a.group(2)), url)
        a = re.search(r'人民币贷款余额[\d.]+万亿元,同比(增长|下降)' + NUM + '%', t)
        if a: put('cn_loan', per, signed(a.group(1), a.group(2)), url)


# ---------------------------------------------------------------- OECD
OECD_KEY = 'CHN+USA+EA20+JPN.M+Q.......'
OECD_API = 'https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_KEI@DF_KEI,4.0/{k}?startPeriod=2015-01&format=csvfilewithlabels'
OECD_UI = 'https://data-explorer.oecd.org/vis?df[ds]=dsDisseminateFinalDMZ&df[id]=DSD_KEI%40DF_KEI&df[ag]=OECD.SDD.STES&dq={k}'
OECD_SERIES = {  # sid: (area, freq, measure, unit, activity, adjustment, transformation)
    'cn_gdp': ('CHN', 'Q', 'B1GQ_Q', 'GR', '_T', 'Y', 'GY'),
    'cn_exp': ('CHN', 'M', 'EX', 'GR', '_T', 'Y', 'GY'),
    'cn_imp': ('CHN', 'M', 'IM', 'GR', '_T', 'Y', 'GY'),
    'cn_10y': ('CHN', 'M', 'IRLT', 'PA', '_Z', '_Z', '_Z'),
    'cn_fx': ('CHN', 'M', 'CC', 'XDC_USD', '_Z', '_Z', '_Z'),
    'us_gdp': ('USA', 'Q', 'B1GQ_Q', 'GR', '_T', 'Y', 'GY'),
    'us_ip': ('USA', 'M', 'PRVM', 'GR', 'BTE', 'Y', 'GY'),
    'us_retail': ('USA', 'M', 'TOVM', 'GR', 'G47', 'Y', 'GY'),
    'us_cpi': ('USA', 'M', 'CP', 'GR', '_Z', '_Z', 'GY'),
    'us_wage': ('USA', 'M', 'H_EARN', 'GR', 'C', 'Y', 'GY'),
    'us_ur': ('USA', 'M', 'UNEMP', 'PT_LF', '_T', 'Y', '_Z'),
    'us_ffr': ('USA', 'M', 'IRSTCI', 'PA', '_Z', '_Z', '_Z'),
    'us_10y': ('USA', 'M', 'IRLT', 'PA', '_Z', '_Z', '_Z'),
    'us_exp': ('USA', 'M', 'EX', 'GR', '_T', 'Y', 'GY'),
    'ea_gdp': ('EA20', 'Q', 'B1GQ_Q', 'GR', '_T', 'Y', 'GY'),
    'ea_ip': ('EA20', 'M', 'PRVM', 'GR', 'BTE', 'Y', 'GY'),
    'ea_retail': ('EA20', 'M', 'TOVM', 'GR', 'G47', 'Y', 'GY'),
    'ea_hicp': ('EA20', 'M', 'CP', 'GR', '_Z', '_Z', 'GY'),
    'ea_rate': ('EA20', 'M', 'IRSTCI', 'PA', '_Z', '_Z', '_Z'),
    'ea_10y': ('EA20', 'M', 'IRLT', 'PA', '_Z', '_Z', '_Z'),
    'ea_m3': ('EA20', 'M', 'MABM', 'GR', '_Z', 'Y', 'GY'),
    'jp_gdp': ('JPN', 'Q', 'B1GQ_Q', 'GR', '_T', 'Y', 'GY'),
    'jp_ip': ('JPN', 'M', 'PRVM', 'GR', 'BTE', 'Y', 'GY'),
    'jp_ur': ('JPN', 'M', 'UNEMP', 'PT_LF', '_T', 'Y', '_Z'),
    'jp_exp': ('JPN', 'M', 'EX', 'GR', '_T', 'Y', 'GY'),
    'jp_call': ('JPN', 'M', 'IRSTCI', 'PA', '_Z', '_Z', '_Z'),
    'jp_10y': ('JPN', 'M', 'IRLT', 'PA', '_Z', '_Z', '_Z'),
    'jp_fx': ('JPN', 'M', 'CC', 'XDC_USD', '_Z', '_Z', '_Z'),
}
OECD_LINKS = {}
# 统计局发布稿只能回溯到 2021 年底，更早的月份用 OECD 同口径序列补齐
OECD_BACKFILL = {'cn_cpi': ('CHN', 'M', 'CP', 'GR', '_Z', '_Z', 'GY')}


def oecd():
    text = fetch(OECD_API.format(k=OECD_KEY), cache=False, timeout=180)
    if not text:
        print('  ! OECD 接口未返回，沿用上次数据')
        return False
    rows = list(csv.DictReader(io.StringIO(text)))
    for sid, spec in OECD_SERIES.items():
        key = '.'.join(spec)
        OECD_LINKS[sid] = (OECD_API.format(k=key), OECD_UI.format(k=key))
        for x in rows:
            if (x['REF_AREA'], x['FREQ'], x['MEASURE'], x['UNIT_MEASURE'], x['ACTIVITY'], x['ADJUSTMENT'], x['TRANSFORMATION']) == spec and x['OBS_VALUE']:
                d = x['TIME_PERIOD']
                if spec[1] == 'Q':
                    d = d[:4] + '-%02d' % (int(d[-1]) * 3)
                put(sid, d, float(x['OBS_VALUE']), '')
    for sid, spec in OECD_BACKFILL.items():
        key = '.'.join(spec)
        OECD_LINKS.setdefault(sid, (OECD_API.format(k=key), OECD_UI.format(k=key)))
        for x in rows:
            if (x['REF_AREA'], x['FREQ'], x['MEASURE'], x['UNIT_MEASURE'], x['ACTIVITY'], x['ADJUSTMENT'], x['TRANSFORMATION']) == spec and x['OBS_VALUE']:
                if (sid, x['TIME_PERIOD']) not in OBS:
                    put(sid, x['TIME_PERIOD'], round(float(x['OBS_VALUE']), 1), '')
    return True


# ---------------------------------------------------------------- 券商研报
# 主来源：新浪财经研报中心（覆盖招商、申万、光大、浙商及外资投行等，报告页写明机构、研究员与核心观点）
# 补充：东方财富研报中心（附 PDF 原文）。收录记录保存在 content/research_index.json，随仓库累积。
SINA_LIST = 'https://stock.finance.sina.com.cn/stock/go.php/vReport_List/kind/{k}/index.phtml?p={p}'
SINA_KIND = {'macro': '宏观', 'strategy': '策略'}
EM_API = ('https://reportapi.eastmoney.com/report/jg?pageSize=100&beginTime={b}&endTime={e}&pageNo={p}'
          '&fields=&qType={q}&orgCode=&author=&p={p}&pageNum={p}&pageNumber={p}')
EM_PAGE = {3: 'https://data.eastmoney.com/report/zw_macresearch.jshtml?encodeUrl=', 2: 'https://data.eastmoney.com/report/zw_strategy.jshtml?encodeUrl='}
EM_TYPE = {3: '宏观', 2: '策略'}
FOREIGN = {'摩根士丹利': '摩根士丹利', '高盛': '高盛', '摩根大通': '摩根大通', '瑞银': '瑞银', '花旗': '花旗', '美银': '美银', '野村': '野村',
           '汇丰': '汇丰', '德意志银行': '德意志银行', '巴克莱': '巴克莱', '星展': '星展', '大华继显': '大华继显'}
SKIP_TITLE = ('日报', '晨报', '早报', '晚报', '每日', '晨会', '早参', '午评', '收评', '资讯', '鑫新闻', '新股纵览')
REPORT_DAYS = 365


def org_short(name):
    name = name.strip()
    for k, v in FOREIGN.items():
        if name.startswith(k):
            return v
    if name.startswith('上海申银万国证券研究所'):
        return '申万宏源'
    for suf in ('股份有限公司', '有限责任公司', '有限公司', '(香港)', '（香港）'):
        name = name.replace(suf, '')
    return name


def keep_report(org, title):
    if any(k in title for k in SKIP_TITLE):
        return False
    return ('证券' in org or org.startswith('中金') or org in FOREIGN.values() or org == '申万宏源') and '期货' not in org


def _decode(raw):
    for enc in ('gbk', 'utf-8'):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode('utf-8', 'ignore')


def sina_page(url):
    key = os.path.join(CACHE, hashlib.md5(url.encode()).hexdigest() + '.html')
    if os.path.exists(key):
        return open(key, encoding='utf-8').read()
    for attempt in range(3):
        try:
            raw = _direct.open(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=30).read()
            text = _decode(raw)
            _mark(url, True)
            if 'vReport_Show' in url and len(text) > 3000:
                open(key, 'w', encoding='utf-8').write(text)
            time.sleep(0.2)
            return text
        except Exception:
            time.sleep(2 * (attempt + 1))
    _mark(url, False)
    return ''


def _source_extra(url):
    return '新浪研报' if 'sina.com.cn' in url else None


def reports():
    from datetime import timedelta
    path = os.path.join(CONTENT, 'research_index.json')
    have = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    cutoff = str(today - timedelta(days=REPORT_DAYS))
    stop = str(today - timedelta(days=10)) if have and not FULL else cutoff
    # 新浪：逐页往前翻，翻到已收录日期之前为止
    for kind, label in SINA_KIND.items():
        for page in range(1, 60):
            t = sina_page(SINA_LIST.format(k=kind, p=page))
            rows = re.findall(r'<tr>(.*?)</tr>', t, re.S)
            got = 0; oldest = '9999'
            for r in rows:
                a = re.search(r'title="([^"]+)"\s+href="([^"]*vReport_Show[^"]*)"', r)
                d = re.search(r'<td>(\d{4}-\d{2}-\d{2})</td>', r)
                o = re.search(r'fname05"><span>(.*?)</span>', r)
                if not (a and d and o):
                    continue
                got += 1; oldest = min(oldest, d.group(1))
                title, url = html.unescape(a.group(1)).strip(), 'https:' + a.group(2) if a.group(2).startswith('//') else a.group(2)
                org = org_short(html.unescape(o.group(1)))
                au = re.search(r'class="fname"><span>(.*?)</span>', r)
                if not keep_report(org, title):
                    continue
                rid = 's' + re.search(r'rptid/(\d+)', url).group(1)
                have.setdefault(rid, dict(date=d.group(1), institution=org, type=label, title=title,
                                          authors=(au.group(1) if au else '').replace('/', '、'), url=url, pdf=''))
            if not got or oldest < stop:
                break
    # 东方财富：补充新浪未收录的报告，并给同名报告补上 PDF
    seen = {(v['institution'], v['title']): k for k, v in have.items()}
    for q in (3, 2):
        for page in range(1, 40):
            text = fetch(EM_API.format(b=stop, e=today, p=page, q=q), cache=False)
            try:
                rows = json.loads(text).get('data') or []
            except Exception:
                rows = []
            if not rows:
                break
            for r in rows:
                org, title, code = org_short(r.get('orgSName') or ''), (r.get('title') or '').strip(), r.get('encodeUrl')
                if not code or not keep_report(org, title):
                    continue
                url = EM_PAGE[q] + urllib.parse.quote(code, safe='')
                if (org, title) in seen:
                    have[seen[(org, title)]].setdefault('em_url', url)
                    continue
                rid = 'e' + hashlib.md5(code.encode()).hexdigest()[:12]
                have.setdefault(rid, dict(date=(r.get('publishDate') or '')[:10], institution=org, type=EM_TYPE[q], title=title,
                                          authors=(r.get('researcher') or '').replace(',', '、'), url=url, pdf=''))
                seen[(org, title)] = rid
    em_cut = str(today - timedelta(days=120))       # 东方财富以中小券商为主，只保留近 120 天
    have = {k: v for k, v in have.items() if v['date'] >= (cutoff if k.startswith('s') else em_cut)}
    for v in have.values():                          # 旧版本误取到网页样式文字的摘要，重新读取
        if v.get('summary', '').startswith('blk_container'):
            v.pop('summary')
    # 读取新收录报告的正文摘要
    todo = [v for v in have.values() if 'summary' not in v]
    print('  新收录研报 %d 篇' % len(todo), flush=True)
    def detail(v):
        if 'sina.com.cn' in v['url']:
            t = sina_page(v['url'])
            i = t.find('class="blk_container"')
            j = t.find('</div>', i) if i > 0 else -1
            body = html.unescape(re.sub(r'<[^>]+>', ' ', t[t.find('>', i) + 1:j])) if i > 0 else ''
            body = re.sub(r'\s+', ' ', body).strip()
            v['summary'] = body[:200] + ('…' if len(body) > 200 else '')
        else:
            t = fetch(v['url'])
            m = re.search(r'"notice_content":"(.*?)","', t, re.S)
            s = json.loads('"' + m.group(1) + '"') if m else ''
            s = re.sub(r'\s+', ' ', s).strip()
            v['summary'] = s[:200] + ('…' if len(s) > 200 else '')
            p = re.search(r'"attach_url":"(https?://pdf\.dfcfw\.com/[^"]+)"', t)
            v['pdf'] = p.group(1) if p else ''
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(detail, todo))
    json.dump(have, open(path, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    out = [dict(rid=k, **v) for k, v in have.items()]
    out.sort(key=lambda x: (x['date'], x['rid']), reverse=True)
    print('  研报库共 %d 篇' % len(out))
    return out


# ---------------------------------------------------------------- 指标目录
NBS_DB = 'https://data.stats.gov.cn/easyquery.htm?cn=A01'
NBS_REL = 'https://www.stats.gov.cn/sj/zxfb/'
PBC_DB = 'https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html'
PBC_REL = 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html'
C = []


def cat(sid, region, country, category, name, unit, freq, origin, db_name, db_url, rel_url='', note=''):
    api, ui = OECD_LINKS.get(sid, ('', ''))
    C.append(dict(sid=sid, region=region, country=country, category=category, name=name, unit=unit, freq=freq,
                  origin=origin, db_name=db_name, db_url=db_url, rel_url=rel_url, api_url=api, oecd_url=ui, note=note))


def build_catalog():
    S = '国家统计局'; P = '中国人民银行'
    cat('cn_gdp', '国内', '中国', '增长', 'GDP 不变价当季同比', '%', '季', S, '国家数据 · 季度', 'https://data.stats.gov.cn/easyquery.htm?cn=B01', NBS_REL, '季度值记在季末月；OECD 按官方数据整理')
    cat('cn_ip', '国内', '中国', '增长', '规上工业增加值 当月同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL, '1—2 月为合并值，记在 2 月')
    cat('cn_retail', '国内', '中国', '增长', '社零总额 当月同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL, '名义增速')
    cat('cn_retail_ytd', '国内', '中国', '增长', '社零总额 累计同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL, '名义增速')
    cat('cn_fai', '国内', '中国', '增长', '固定资产投资 累计同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL, '不含农户，按可比口径')
    cat('cn_fai_mfg', '国内', '中国', '增长', '制造业投资 累计同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_fai_infra', '国内', '中国', '增长', '基础设施投资 累计同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_re_inv', '国内', '中国', '地产', '房地产开发投资 累计同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_re_sales', '国内', '中国', '地产', '新建商品房销售面积 累计同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_profit', '国内', '中国', '增长', '规上工业企业利润 累计同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_ur', '国内', '中国', '就业', '城镇调查失业率', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_cpi', '国内', '中国', '价格', 'CPI 同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL, '2021 年 11 月起取自统计局发布稿，此前取自 OECD 数据库')
    cat('cn_core_cpi', '国内', '中国', '价格', '核心 CPI 同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL, '扣除食品和能源')
    cat('cn_cpi_mom', '国内', '中国', '价格', 'CPI 环比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_ppi', '国内', '中国', '价格', 'PPI 同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_ppirm', '国内', '中国', '价格', 'PPIRM 购进价格同比', '%', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_pmi', '国内', '中国', '景气', '制造业 PMI', '', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL, '50 为荣枯线')
    cat('cn_pmi_no', '国内', '中国', '景气', 'PMI 新订单指数', '', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_npmi', '国内', '中国', '景气', '非制造业商务活动指数', '', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_cpmi', '国内', '中国', '景气', '综合 PMI 产出指数', '', '月', S, '国家数据 · 月度', NBS_DB, NBS_REL)
    cat('cn_exp', '国内', '中国', '外贸', '出口金额同比（美元）', '%', '月', '海关总署', '海关统计数据在线查询', 'http://stats.customs.gov.cn/', 'http://www.customs.gov.cn/customs/302249/zfxxgk/2799825/302274/302275/index.html', 'OECD 按出口金额计算，与海关公布值可能有 0.1—0.3 个百分点差异')
    cat('cn_imp', '国内', '中国', '外贸', '进口金额同比（美元）', '%', '月', '海关总署', '海关统计数据在线查询', 'http://stats.customs.gov.cn/', '', 'OECD 按进口金额计算')
    cat('cn_m2', '国内', '中国', '金融', 'M2 同比', '%', '月', P, '人民银行 · 统计数据', PBC_DB, PBC_REL)
    cat('cn_m1', '国内', '中国', '金融', 'M1 同比', '%', '月', P, '人民银行 · 统计数据', PBC_DB, PBC_REL, '2025 年起 M1 口径扩充')
    cat('cn_tsf', '国内', '中国', '金融', '社融存量 同比', '%', '月', P, '人民银行 · 统计数据', PBC_DB, PBC_REL)
    cat('cn_loan', '国内', '中国', '金融', '人民币贷款余额 同比', '%', '月', P, '人民银行 · 统计数据', PBC_DB, PBC_REL)
    cat('cn_10y', '国内', '中国', '利率汇率', '10 年期国债收益率（月均）', '%', '月', '中央结算公司', '中国债券信息网 · 收益率曲线', 'https://yield.chinabond.com.cn/')
    cat('cn_fx', '国内', '中国', '利率汇率', '人民币兑美元（月均）', '元', '月', '中国外汇交易中心', '中国货币网 · 汇率中间价', 'https://www.chinamoney.com.cn/chinese/bkccpr/')
    cat('us_gdp', '海外', '美国', '增长', '美国 GDP 实际同比', '%', '季', '美国经济分析局', 'BEA · GDP', 'https://www.bea.gov/data/gdp/gross-domestic-product', '', '同比口径，非季调环比折年率')
    cat('us_ip', '海外', '美国', '增长', '美国工业产出同比', '%', '月', '美联储', 'FRED · INDPRO', 'https://fred.stlouisfed.org/series/INDPRO')
    cat('us_retail', '海外', '美国', '增长', '美国零售销售量同比', '%', '月', '美国人口普查局', 'Census · 月度零售', 'https://www.census.gov/economic-indicators/', '', '实际量口径')
    cat('us_exp', '海外', '美国', '外贸', '美国出口同比', '%', '月', '美国人口普查局', 'Census · 国际贸易', 'https://www.census.gov/economic-indicators/')
    cat('us_cpi', '海外', '美国', '价格', '美国 CPI 同比', '%', '月', '美国劳工统计局', 'BLS · CPI-U', 'https://data.bls.gov/timeseries/CUUR0000SA0', 'https://www.bls.gov/cpi/', 'OECD 按指数计算，与 BLS 公布值可能有小数位差异')
    cat('us_wage', '海外', '美国', '就业', '美国制造业时薪同比', '%', '月', '美国劳工统计局', 'BLS · 就业统计', 'https://www.bls.gov/ces/')
    cat('us_ur', '海外', '美国', '就业', '美国失业率', '%', '月', '美国劳工统计局', 'BLS · LNS14000000', 'https://data.bls.gov/timeseries/LNS14000000', 'https://www.bls.gov/news.release/empsit.nr0.htm')
    cat('us_ffr', '海外', '美国', '利率汇率', '联邦基金有效利率（月均）', '%', '月', '美联储', 'FRED · EFFR', 'https://fred.stlouisfed.org/series/EFFR', 'https://www.federalreserve.gov/monetarypolicy/openmarket.htm')
    cat('us_10y', '海外', '美国', '利率汇率', '美国 10 年期国债收益率（月均）', '%', '月', '美国财政部', '财政部 · 国债收益率', 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve')
    cat('ea_gdp', '海外', '欧元区', '增长', '欧元区 GDP 实际同比', '%', '季', 'Eurostat', 'Eurostat · 季度国民账户', 'https://ec.europa.eu/eurostat/databrowser/view/namq_10_gdp/default/table')
    cat('ea_ip', '海外', '欧元区', '增长', '欧元区工业产出同比', '%', '月', 'Eurostat', 'Eurostat · 工业生产', 'https://ec.europa.eu/eurostat/databrowser/view/sts_inpr_m/default/table')
    cat('ea_retail', '海外', '欧元区', '增长', '欧元区零售量同比', '%', '月', 'Eurostat', 'Eurostat · 零售贸易', 'https://ec.europa.eu/eurostat/databrowser/view/sts_trtu_m/default/table')
    cat('ea_hicp', '海外', '欧元区', '价格', '欧元区 HICP 同比', '%', '月', 'Eurostat', 'Eurostat · HICP', 'https://ec.europa.eu/eurostat/databrowser/view/prc_hicp_manr/default/table')
    cat('ea_rate', '海外', '欧元区', '利率汇率', '欧元短期利率 €STR（月均）', '%', '月', '欧洲央行', 'ECB 数据门户 · €STR', 'https://data.ecb.europa.eu/data/datasets/EST')
    cat('ea_10y', '海外', '欧元区', '利率汇率', '欧元区 10 年期国债收益率（月均）', '%', '月', '欧洲央行', 'ECB 数据门户 · 收益率曲线', 'https://data.ecb.europa.eu/data/datasets/YC')
    cat('ea_m3', '海外', '欧元区', '金融', '欧元区 M3 同比', '%', '月', '欧洲央行', 'ECB 数据门户 · 货币总量', 'https://data.ecb.europa.eu/data/datasets/BSI')
    cat('jp_gdp', '海外', '日本', '增长', '日本 GDP 实际同比', '%', '季', '日本内阁府', '内阁府 · 国民经济计算', 'https://www.esri.cao.go.jp/en/sna/menu.html')
    cat('jp_ip', '海外', '日本', '增长', '日本工业产出同比', '%', '月', '日本经济产业省', 'METI · 工矿业生产', 'https://www.meti.go.jp/english/statistics/tyo/iip/index.html')
    cat('jp_ur', '海外', '日本', '就业', '日本失业率', '%', '月', '日本总务省', 'e-Stat · 劳动力调查', 'https://www.e-stat.go.jp/en')
    cat('jp_exp', '海外', '日本', '外贸', '日本出口同比（美元）', '%', '月', '日本财务省', '财务省 · 贸易统计', 'https://www.customs.go.jp/toukei/info/index_e.htm')
    cat('jp_call', '海外', '日本', '利率汇率', '日本无担保隔夜拆借利率（月均）', '%', '月', '日本央行', '日本央行 · 时间序列', 'https://www.stat-search.boj.or.jp/index_en.html')
    cat('jp_10y', '海外', '日本', '利率汇率', '日本 10 年期国债收益率（月均）', '%', '月', '日本财务省', '财务省 · 国债利率', 'https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/index.htm')
    cat('jp_fx', '海外', '日本', '利率汇率', '日元兑美元（月均）', '日元', '月', '日本央行', '日本央行 · 时间序列', 'https://www.stat-search.boj.or.jp/index_en.html')


SUPPLEMENT = [
    ('cn_ppi', '2024-12', -2.3, 'https://www.stats.gov.cn/sj/zxfb/202501/t20250109_1958169.html'),
    ('cn_ppi', '2026-03', 0.5, 'https://www.news.cn/20260410/af2415a069494ab4a9cd3e652b08ed0c/c.html'),
]


def build_single():
    # 单文件版：把数据内嵌进网页，方便直接发微信 / 邮件
    page = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    data_js = open(os.path.join(ROOT, 'data.js'), encoding='utf-8').read()
    rpath = os.path.join(ROOT, 'research.js')
    research_js = open(rpath, encoding='utf-8').read() if os.path.exists(rpath) else ''
    inline = page.replace('<script src="data.js"></script>', '<script>' + data_js + '</script><script>' + research_js + '</script>')
    open(os.path.join(ROOT, '宏观策略看板_单文件版.html'), 'w', encoding='utf-8').write(inline)


def fix_links():
    # data.stats.gov.cn 只对中国境内网络开放：主链接改用全球可访问的统计局发布栏目，数据库查询页作为备用
    for c in C:
        if c['db_url'].startswith('https://data.stats.gov.cn'):
            c['alt_url'], c['alt_name'] = c['db_url'], '国家数据库查询（仅限境内网络）'
            c['db_url'], c['db_name'] = NBS_REL, '国家统计局 · 最新发布'
            c['rel_url'] = ''


def main():
    if '--build' in sys.argv:              # 只改了网页或观点/事件时：不联网，直接重新打包
        prev = json.loads(open(os.path.join(ROOT, 'data.js'), encoding='utf-8').read().split('=', 1)[1].rstrip().rstrip(';'))
        prev['reports'] = json.load(open(os.path.join(CONTENT, 'reports.json'), encoding='utf-8'))
        prev['events'] = json.load(open(os.path.join(CONTENT, 'events.json'), encoding='utf-8'))
        have = {c['sid'] for c in prev['catalog']}
        OECD_LINKS.update({c['sid']: (c['api_url'], c['oecd_url']) for c in prev['catalog'] if c.get('api_url')})
        build_catalog()
        prev['catalog'] = [c for c in C if c['sid'] in have]
        if 'research' in prev:
            open(os.path.join(ROOT, 'research.js'), 'w', encoding='utf-8').write('window.MACRO_RESEARCH = ' + json.dumps(prev.pop('research'), ensure_ascii=False) + ';\n')
        open(os.path.join(ROOT, 'data.js'), 'w', encoding='utf-8').write('window.MACRO_DATA = ' + json.dumps(prev, ensure_ascii=False) + ';\n')
        build_single()
        print('已重新打包（未联网更新数据）')
        return
    prev_path = os.path.join(ROOT, 'data.js')
    prev = {}
    if os.path.exists(prev_path):          # 某个来源这次抓不到时沿用上次的数据
        try:
            prev = json.loads(open(prev_path, encoding='utf-8').read().split('=', 1)[1].rstrip().rstrip(';'))
        except Exception:
            prev = {}
    print('· 国家统计局', flush=True); parse_nbs(nbs_list())
    print('· 人民银行', flush=True); pbc()
    print('· OECD', flush=True); ok = oecd()
    print('· 券商研报', flush=True)
    try:
        auto = reports()
    except Exception as e:
        print('  ! 研报抓取失败，沿用上次：', e)
        auto = []
    if not auto:
        try:
            auto = json.loads(open(os.path.join(ROOT, 'research.js'), encoding='utf-8').read().split('=', 1)[1].rstrip().rstrip(';'))
        except Exception:
            auto = []
    if not ok and prev:
        for o in prev.get('observations', []):
            if o['sid'] in OECD_SERIES:
                put(o['sid'], o['date'], o['value'], o['src'])
        for c in prev.get('catalog', []):
            if c['sid'] in OECD_SERIES:
                OECD_LINKS[c['sid']] = (c['api_url'], c['oecd_url'])
    # 某个网站这次连不上时，保留上次已有的数据点，避免历史数据被清空
    kept = 0
    for o in prev.get('observations', []):
        if (o['sid'], o['date']) not in OBS:
            put(o['sid'], o['date'], o['value'], o['src']); kept += 1
    if kept:
        print('  沿用上次数据 %d 个点' % kept)
    # 少数发布稿正文无法抓取，按官方发布内容补录（仅在自动抓取缺失时使用）
    for sid, date, value, src in SUPPLEMENT:
        if (sid, date) not in OBS:
            put(sid, date, value, src)
    build_catalog()
    obs = sorted((o for o in OBS.values() if o['date'] >= '2015-01'), key=lambda o: (o['sid'], o['date']))
    have = {o['sid'] for o in obs}
    catalog = [c for c in C if c['sid'] in have]
    data = dict(
        updated=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M'),
        sources=STATUS,
        catalog=catalog, observations=obs,
        reports=json.load(open(os.path.join(CONTENT, 'reports.json'), encoding='utf-8')),
        research=auto,
        events=json.load(open(os.path.join(CONTENT, 'events.json'), encoding='utf-8')),
    )
    research = data.pop('research')
    open(prev_path, 'w', encoding='utf-8').write('window.MACRO_DATA = ' + json.dumps(data, ensure_ascii=False) + ';\n')
    open(os.path.join(ROOT, 'research.js'), 'w', encoding='utf-8').write('window.MACRO_RESEARCH = ' + json.dumps(research, ensure_ascii=False) + ';\n')
    build_single()
    for k, v in STATUS.items():
        print('  来源 %-8s 成功 %d 次，失败 %d 次' % (k, v['ok'], v['fail']))
    print('完成：%d 个指标，%d 个数据点，数据更新于 %s' % (len(catalog), len(obs), data['updated']))
    for c in catalog:
        ds = [o['date'] for o in obs if o['sid'] == c['sid']]
        print('  %-14s %-24s %3d 期  %s → %s' % (c['sid'], c['name'], len(ds), ds[0], ds[-1]))


if __name__ == '__main__':
    main()
