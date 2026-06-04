"""
文件名解析模块
支持: 字幕组前缀 / 番剧 - 01 / S01E01 / 中文季集 / 1x05 / 多集范围 / GM-Team 多标签 等
"""
import re
from pathlib import Path


# 已知分类标签 (跳过, 不当作标题)
_CATEGORIES = frozenset({
    '国漫', '日漫', '美漫', '动漫', '番剧', 'TV', 'OVA', 'ONA', 'MOVIE',
    '电影', '剧集', '综艺', '完结', '完整', '合集', 'Multiple Subtitle',
    'AVC', 'HEVC', 'AAC', 'AC3', 'FLAC', 'CHS', 'CHT', 'GB', 'BIG5',
    'JPN', 'JP', 'CN', 'EN', '简繁', '简日', '简体', '繁体', '内嵌',
    'WebRip', 'WEB-DL', 'BluRay', 'BDRip', '10bit', '8bit',
    'x264', 'x265', 'H264', 'H265', 'H.264', 'H.265',
})

_SKIP_CN = frozenset({'国漫', '日漫', '动漫', '美漫', '番剧', '电影', '剧集', '综艺'})

# 中文数字映射
_CN_NUM = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '十一': 11, '十二': 12, '十三': 13, '十四': 14, '十五': 15,
    '十六': 16, '十七': 17, '十八': 18, '十九': 19, '二十': 20,
}

# 技术标签列表 (用于从标题中剥离)
_TECH_TOKENS = [
    'WEB-DL', 'WEBRip', 'WEB', 'BluRay', 'BDRip', 'BRRip', 'DVDRip', 'HDRip',
    'HDTV', 'PDTV', 'NF', 'AMZN', 'DSNP', 'iT', 'HMAX', 'ATVP',
    'x264', 'x265', 'H264', 'H265', 'H\\.264', 'H\\.265', 'HEVC', 'AVC', '10bit',
    'AAC', 'AC3', 'DTS', 'DDP', 'DD\\+', 'DD', 'DTS-HD', 'TrueHD', 'Atmos', 'FLAC', 'OPUS',
    '5\\.1', '7\\.1', '2\\.0', 'MA',
    'HDR', 'HDR10', 'HDR10\\+', 'DV', 'Dolby Vision', 'SDR',
    'PROPER', 'REPACK', 'INTERNAL', 'LIMITED', 'EXTENDED', 'UNCUT', 'UNRATED', 'REMASTERED',
    '4K', '8K', 'UHD', 'BDMV', 'IMAX', 'REMUX',
    'CHT', 'CHS', 'GB', 'BIG5', 'JP', 'EN',
    'Multiple Subtitle', 'Subtitle', 'sub',
    'ColorWEB', 'FRDS', 'RARBG', 'QiQi', 'CatchPlay',
]


def _cn_to_int(s: str) -> int:
    """中文数字或阿拉伯数字 → int"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    # 处理 "二十一" ~ "九十九" 等复合
    if '十' in s:
        parts = s.split('十')
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return int(s) if s.isdigit() else 1


def _parse_gm_team_style(name: str, info: dict) -> bool:
    """解析 GM-Team 风格: 开头连续多个 [xxx][yyy][zzz]。返回 True 表示已完成解析。"""
    bracket_tokens = []
    bracket_pattern = re.compile(r'^\s*(?:\[([^\]]+)\]\s*)+')
    if not bracket_pattern.match(name):
        return False

    consumed = 0
    for m in re.finditer(r'\[([^\]]+)\]', name):
        if m.start() != consumed:
            break
        bracket_tokens.append(m.group(1).strip())
        consumed = m.end()

    if len(bracket_tokens) < 3:
        return False

    info["is_anime"] = True

    for tok in bracket_tokens[1:]:  # 跳过第 0 个 (字幕组)
        tok_clean = tok.strip()
        # 分辨率
        if not info["resolution"]:
            m = re.match(r'^(2160|1080|720|480)[pPiI]$', tok_clean)
            if m:
                info["resolution"] = tok_clean.lower()
                continue
            if re.match(r'^[48]K$', tok_clean, re.IGNORECASE):
                info["resolution"] = tok_clean.upper()
                continue
        # 年份
        if not info["year"]:
            m = re.match(r'^(19[5-9]\d|20\d{2})$', tok_clean)
            if m:
                info["year"] = int(m.group(1))
                continue
        # 集号
        if not info["episode"]:
            m = re.match(r'^(\d{1,4})(?:[-vV]?\d{0,4})?$', tok_clean)
            if m:
                ep = int(m.group(1))
                if not (1950 <= ep <= 2099):
                    info["episode"] = ep
                    info["season"] = info["season"] or 1
                    continue
            m = re.match(r'^第\s*(\d{1,4})\s*[集话]', tok_clean)
            if m:
                info["episode"] = int(m.group(1))
                info["season"] = info["season"] or 1
                continue
        # 跳过分类
        if tok_clean in _CATEGORIES:
            continue
        # CRC hash 跳过
        if re.match(r'^[0-9A-Fa-f]{6,10}$', tok_clean) and len(tok_clean) >= 8:
            continue
        # 中文标题
        if re.search(r'[\u4e00-\u9fff]', tok_clean) and not info["title_cn"]:
            info["title_cn"] = tok_clean
            continue
        # 英文标题
        if re.search(r'[A-Za-z]', tok_clean) and not info["title_en"]:
            info["title_en"] = tok_clean
            continue

    return bool(info["title_cn"] or info["title_en"])


def _parse_sub_group_prefix(name: str, work: str, info: dict) -> str:
    """解析单个字幕组前缀 [xxx]，返回剥离后的 work 字符串。"""
    sub_group = re.match(r'^\s*\[([^\]]+)\]\s*', work)
    if not sub_group:
        return work

    info["is_anime"] = True
    work = work[sub_group.end():]

    # 从方括号里抓集号
    if not info["episode"]:
        for m in re.finditer(r'\[(\d{1,4})(?:[vV]\d)?\]', work):
            ep = int(m.group(1))
            if 0 < ep < 5000 and not (1950 <= ep <= 2099):
                info["episode"] = ep
                info["season"] = info["season"] or 1
                break
    # Title - NNvN 格式
    if not info["episode"]:
        m = re.search(r'[-—]\s*(\d{1,4})[vV]?\d?\s*(?:\[|$)', work)
        if m:
            info["episode"] = int(m.group(1))
            info["season"] = info["season"] or 1

    # "中文标题 / 英文标题 - 01" 格式
    if '/' in work or '／' in work:
        parts_slash = re.split(r'\s*[/／]\s*', work, maxsplit=1)
        if len(parts_slash) == 2:
            cn_part = parts_slash[0].strip()
            en_part = parts_slash[1].strip()
            if re.search(r'[\u4e00-\u9fff]', cn_part) and not info.get("title_cn"):
                info["title_cn"] = cn_part.strip()
            if re.search(r'[A-Za-z]', en_part):
                m2 = re.match(r'(.+?)\s*[-—]\s*\d{1,4}', en_part)
                if m2:
                    if not info.get("title_en"):
                        info["title_en"] = m2.group(1).strip()
                else:
                    if not info.get("title_en"):
                        info["title_en"] = re.sub(r'\s*\[.*', '', en_part).strip()

    # 从去括号后的 work 拿中文标题
    if not info.get("title_cn"):
        cleaned = re.sub(r'\[[^\]]*\]', '', work).strip()
        cleaned = re.sub(r'\s*[-—]\s*\d{1,4}[vV]?\d?\s*$', '', cleaned).strip()
        if re.search(r'[\u4e00-\u9fff]', cleaned):
            info["title_cn"] = cleaned.strip()

    # 从方括号抓分辨率
    if not info["resolution"]:
        m = re.search(r'\[(2160|1080|720|480)[pPiI]\]', work)
        if m:
            info["resolution"] = (m.group(1) + 'p').lower()

    return work


def _parse_slash_titles(work: str, info: dict):
    """解析 "标题 / 英文标题" 格式。"""
    if info.get("title_cn") or info.get("title_en"):
        return
    if '/' not in work and '／' not in work:
        return

    parts_slash = re.split(r'\s*[/／]\s*', work, maxsplit=1)
    if len(parts_slash) != 2:
        return

    cn_part = parts_slash[0].strip()
    en_part = parts_slash[1].strip()
    if re.search(r'[\u4e00-\u9fff]', cn_part):
        info["title_cn"] = cn_part.strip()
    if re.search(r'[A-Za-z]', en_part):
        m = re.match(r'(.+?)\s*[-—]\s*(\d{1,4})', en_part)
        if m:
            if not info.get("title_en"):
                info["title_en"] = m.group(1).strip()
            if not info["episode"]:
                info["episode"] = int(m.group(2))
                info["season"] = info["season"] or 1
        else:
            if not info.get("title_en"):
                info["title_en"] = en_part


def _parse_season_episode(work: str, name: str, info: dict):
    """从 work 中解析季集信息（多种格式）。"""
    # 3.1 标准 S01E01
    m = re.search(
        r'(?:^|[^A-Za-z\d])S(\d{1,2})\s*[\.\s_-]*\s*E(\d{1,4})(?:\s*[-Ee]?\s*E?(\d{1,4}))?',
        work, re.IGNORECASE
    )
    if m:
        info["season"] = int(m.group(1))
        info["episode"] = int(m.group(2))
        return

    # 3.2 中文 第N季第M集
    if info["episode"] is None:
        m = re.search(r'第\s*([一二三四五六七八九十\d]+)\s*季[\s\.\-]*第\s*(\d{1,4})\s*[集话]', work)
        if m:
            info["season"] = _cn_to_int(m.group(1))
            info["episode"] = int(m.group(2))
            return

    # 3.2b 第X季.第Y集 分开写
    if info["episode"] is None:
        m = re.search(r'第\s*([一二三四五六七八九十\d]+)\s*季', work)
        if m:
            info["season"] = _cn_to_int(m.group(1))
        m2 = re.search(r'第\s*(\d{1,4})\s*[集话]', work)
        if m2:
            info["episode"] = int(m2.group(1))
            if not info["season"]:
                info["season"] = 1
            return

    # 3.3 中文 第M集/第M话
    if info["episode"] is None:
        m = re.search(r'第\s*(\d{1,4})\s*[集话]', work)
        if m:
            info["episode"] = int(m.group(1))
            info["season"] = info["season"] or 1
            return

    # 3.4 NxM 格式
    if info["episode"] is None:
        m = re.search(r'(?<![\d])(\d{1,2})x(\d{1,4})(?![\d])', work)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            if s <= 50 and e <= 9999:
                info["season"] = s
                info["episode"] = e
                return

    # 3.5 Season X Episode Y
    if info["episode"] is None:
        m = re.search(r'Season[\s\._]*(\d{1,2})[\s\._]+Episode[\s\._]*(\d{1,4})', work, re.IGNORECASE)
        if m:
            info["season"] = int(m.group(1))
            info["episode"] = int(m.group(2))
            return

    # 3.6 番剧 - 01 格式
    if info["episode"] is None and info["is_anime"]:
        m = re.search(r'[-—]\s*(\d{1,4})(?=\s|$|\[)', work)
        if m:
            info["episode"] = int(m.group(1))
            info["season"] = info["season"] or 1
            return

    # 3.7 番剧 - NN [resolution] 格式
    if info["episode"] is None:
        m = re.search(r'\s[-—]\s(\d{1,4})\s*\[', name)
        if m:
            info["episode"] = int(m.group(1))
            info["season"] = info["season"] or 1
            info["is_anime"] = True
            return

    # 3.8 EP01 / E01
    if info["episode"] is None:
        for m in re.finditer(r'(?<![A-Za-z\d])(?:EP|E)[\.\s_-]?(\d{1,4})\b', work, re.IGNORECASE):
            ep = int(m.group(1))
            ctx = m.group(0)
            if ep > 0 and ep < 5000 and not re.search(r'[xXhH]26[45]', ctx):
                info["episode"] = ep
                info["season"] = info["season"] or 1
                return

    # 3.9 末尾纯数字
    if info["episode"] is None and not info["year"]:
        m = re.search(r'\s+(\d{1,4})(?:\s*\[[^\]]*\])?\s*$', name.strip())
        if m:
            ep = int(m.group(1))
            if ep not in (480, 720, 1080, 2160) and ep < 5000:
                info["episode"] = ep
                info["season"] = info["season"] or 1
                info["is_anime"] = True
                return

    # 3.10 中文标题紧贴数字
    if info["episode"] is None and not info["year"]:
        m = re.search(r'[\u4e00-\u9fff](\d{2,4})(?:\s*[\.\[\(\-]|$)', name)
        if m:
            ep = int(m.group(1))
            if ep not in (480, 720, 1080, 2160) and ep < 5000 and not (1950 <= ep <= 2099):
                info["episode"] = ep
                info["season"] = info["season"] or 1


def _extract_titles(work: str, info: dict):
    """从清理后的 work 中提取中英文标题。"""
    title_work = work
    # 替换季集标记
    title_work = re.sub(r'(?<![A-Za-z\d])S\d{1,2}\s*[\.\s_-]*\s*E\d{1,4}(?:\s*[-Ee]?\s*E?\d{1,4})?', ' ', title_work, flags=re.IGNORECASE)
    title_work = re.sub(r'第\s*\d{1,2}\s*季[\s\.\-]*第\s*\d{1,4}\s*[集话]', ' ', title_work)
    title_work = re.sub(r'第\s*\d{1,4}\s*[集话]', ' ', title_work)
    title_work = re.sub(r'(?<![\d])\d{1,2}x\d{1,4}(?![\d])', ' ', title_work)
    title_work = re.sub(r'Season[\s\._]*\d{1,2}[\s\._]+Episode[\s\._]*\d{1,4}', ' ', title_work, flags=re.IGNORECASE)
    title_work = re.sub(r'(?<![A-Za-z\d])(?:EP|E)[\.\s_-]?\d{1,4}\b', ' ', title_work, flags=re.IGNORECASE)
    title_work = re.sub(r'\s[-—]\s\d{1,4}\b', ' ', title_work)

    # 末尾纯数字 (集号)
    if info["episode"] and not re.search(r'(?:S|E|EP|第)', title_work, re.IGNORECASE):
        title_work = re.sub(rf'\s+{info["episode"]}\b', ' ', title_work)
        title_work = re.sub(rf'^{info["episode"]}\s+', ' ', title_work)

    # 替换年份/分辨率
    if info["year"]:
        title_work = re.sub(rf'(?<![\d]){info["year"]}(?![\d])', ' ', title_work)
    if info["resolution"]:
        title_work = re.sub(rf'\b{re.escape(info["resolution"])}\b', ' ', title_work, flags=re.IGNORECASE)

    # 技术标签
    for tok in _TECH_TOKENS:
        title_work = re.sub(rf'\b{tok}\b', ' ', title_work, flags=re.IGNORECASE)
    title_work = re.sub(r'\bPart[\s\.]?\d+\b', ' ', title_work, flags=re.IGNORECASE)
    title_work = re.sub(r'[\.\-_]+', ' ', title_work)
    title_work = re.sub(r'\s+', ' ', title_work).strip()

    # 中文标题
    if not info.get("title_cn"):
        cn_match = re.search(r'([\u4e00-\u9fff][\u4e00-\u9fff\u3000-\u303f\uff01-\uff5e·：:\d]*[\u4e00-\u9fff\d])', title_work)
        if cn_match:
            cn_title = cn_match.group(1).strip()
            if cn_title not in _SKIP_CN:
                info["title_cn"] = cn_title
    if not info.get("title_cn"):
        single = re.search(r'([\u4e00-\u9fff]{2,})', title_work)
        if single and single.group(1) not in {'国漫', '日漫', '动漫'}:
            info["title_cn"] = single.group(1)

    # 去掉中文标题里的季数后缀 (用于 TMDB 搜索)
    if info.get("title_cn"):
        clean = re.sub(r'第[一二三四五六七八九十\d]+季$', '', info["title_cn"]).strip()
        if clean and len(clean) >= 2:
            info["title_cn_search"] = clean
        else:
            info["title_cn_search"] = info["title_cn"]
    else:
        info["title_cn_search"] = None

    # 英文标题
    if not info.get("title_en"):
        en_parts = []
        for token in title_work.split():
            if re.match(r'^\d{4,}$', token):
                continue
            if re.match(r'^\d{0,4}[vV]\d{1,2}$', token):
                continue
            if re.search(r'[\u4e00-\u9fff]', token):
                continue
            if re.search(r'[A-Za-z]', token):
                en_parts.append(token)
        if en_parts:
            info["title_en"] = ' '.join(en_parts).strip()


def parse_filename(filepath: str) -> dict:
    """
    强化版文件名解析 (参考 guessit / Plex 命名规范)
    支持: 字幕组前缀 / 番剧 - 01 / S01E01 / 中文季集 / 1x05 / 多集范围 / GM-Team 多标签 等
    """
    name = Path(filepath).stem
    info = {
        "raw_name": name, "title_cn": None, "title_en": None,
        "season": None, "episode": None, "year": None, "resolution": None,
        "is_anime": False,
    }

    # GM-Team 风格
    if _parse_gm_team_style(name, info):
        return info

    work = name

    # 字幕组前缀
    work = _parse_sub_group_prefix(name, work, info)

    # 斜杠分割标题
    _parse_slash_titles(work, info)

    # 去除圆括号里的分类标签
    work = re.sub(r'[（\(]\s*(?:国漫|日漫|动漫|美漫|番剧|电影|剧集)\s*[）\)]', ' ', work)
    work = re.sub(r'[（\(]\s*(2160|1080|720|480)[pPiI]\s*[）\)]', ' ', work)
    # 末尾的 hash 与小标签
    work = re.sub(r'\[[^\]]*\]', ' ', work)
    work = re.sub(r'-[A-Za-z0-9]{2,12}$', '', work)

    # 分辨率
    res_match = re.search(r'\b(2160|1080|720|480)[pi]\b', work, re.IGNORECASE)
    if res_match:
        info["resolution"] = res_match.group(0).lower()
    elif re.search(r'\b4K\b', work, re.IGNORECASE):
        info["resolution"] = "4K"
    elif re.search(r'\b8K\b', work, re.IGNORECASE):
        info["resolution"] = "8K"

    # 年份
    year_candidates = re.findall(r'(?<![\d])(19[5-9]\d|20\d{2})(?![\d])', work)
    if year_candidates:
        info["year"] = int(year_candidates[-1])

    # 季集
    _parse_season_episode(work, name, info)

    # 提取标题
    _extract_titles(work, info)

    return info
