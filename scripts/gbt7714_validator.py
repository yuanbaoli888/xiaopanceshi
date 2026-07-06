#!/usr/bin/env python3
"""
GB/T 7714-2015 参考文献格式自动校验器 v1.0

基于规则引擎的参考文献格式自动校验。
覆盖期刊[J]、专著[M]、会议[C]、学位论文[D]、网络资源[EB/OL]、报告[R]等全部文献类型。

15条可配置检测规则，逐条标注错误类型、位置和建议修正方案。

用法:
  python gbt7714_validator.py --input refs.txt --output issues.json
"""

import json
import os
import re
import sys
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlparse


# ============================================================
# 参考文献拆分器
# ============================================================
def split_references(content: str) -> List[dict]:
    """
    将参考文献文本拆分为独立条目

    支持两种编号格式：
    [1] ... [2] ...  （GB/T 7714推荐格式）
    1. ... 2. ...    （常见变体）
    """
    refs = []

    # 提取参考文献段落
    ref_section = ''
    section_start = re.search(r'参考\s*文\s*献|References', content, re.IGNORECASE)
    if section_start:
        ref_section = content[section_start.end():]
    else:
        ref_section = content

    # 按编号拆分
    lines = ref_section.split('\n')
    current_num = None
    current_text = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_text:
                current_text.append('')
            continue

        # 匹配编号开头
        match = re.match(r'\s*\[(\d+)\]\s*(.+)', stripped)
        if not match:
            match = re.match(r'\s*(\d+)\.\s*(.+)', stripped)

        if match:
            # 保存上一条
            if current_num is not None and current_text:
                refs.append({
                    'number': current_num,
                    'text': ' '.join(current_text).strip(),
                    'raw_lines': current_text[:],
                })
            current_num = int(match.group(1))
            current_text = [match.group(2).strip()]
        elif current_text:
            current_text.append(stripped)

    # 保存最后一条
    if current_num is not None and current_text:
        refs.append({
            'number': current_num,
            'text': ' '.join(current_text).strip(),
            'raw_lines': current_text[:],
        })

    return refs


# ============================================================
# 文献类型识别
# ============================================================
def identify_ref_type(text: str) -> str:
    """识别参考文献的类型"""
    type_patterns = [
        ('J', r'\[J\]'),
        ('M', r'\[M\]'),
        ('C', r'\[C\]'),
        ('D', r'\[D\]'),
        ('EB_OL', r'\[EB/OL\]'),
        ('R', r'\[R\]'),
        ('N', r'\[N\]'),
        ('P', r'\[P\]'),
        ('Z', r'\[Z\]'),
        ('S', r'\[S\]'),
    ]

    for ref_type, pattern in type_patterns:
        if re.search(pattern, text):
            return ref_type

    # 启发式识别
    if '://' in text and ('http' in text.lower() or 'www.' in text.lower()):
        return 'EB_OL'
    if re.search(r'[（(][Jj][）)]', text):
        return 'J'
    if re.search(r'[（(][Mm][）)]', text):
        return 'M'
    if re.search(r'[（(][Dd][）)]', text):
        return 'D'
    if re.search(r'硕士|博士|学位论文|dissertation|thesis', text, re.IGNORECASE):
        return 'D'
    if re.search(r'出版社|Press|出版', text):
        return 'M'
    if re.search(r'[C]/[//]|会议|conference|proceedings', text, re.IGNORECASE):
        return 'C'

    return 'J'  # 默认按期刊处理


# ============================================================
# 15条检测规则
# ============================================================

class Rule:
    """单条校验规则"""
    def __init__(self, rule_id: str, name: str, description: str, applicable_types: list):
        self.rule_id = rule_id
        self.name = name
        self.description = description
        self.applicable_types = applicable_types

    def check(self, ref: dict) -> Optional[dict]:
        """返回 None 表示通过，返回 dict 表示发现问题"""
        raise NotImplementedError


class Rule01_PeriodNumber(Rule):
    """期号补零检测"""
    def __init__(self):
        super().__init__('r01', '期号补零检测',
                         '期号不得前置补零：如(01)应改为(1)', ['J'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        match = re.search(r'[\(（]0(\d{1})[\)）]', text)
        if match:
            period = match.group(1)
            return {
                'rule': self.rule_id,
                'error': f'期号不当前置补零: ({match.group(0).strip("(（）)")}) → 应为 ({period})',
                'suggestion': f'将"({match.group(0).strip("(（）)")})"改为"({period})"',
                'position': match.start(),
            }
        return None


class Rule02_EnglishAuthor(Rule):
    """英文作者姓名格式"""
    def __init__(self):
        super().__init__('r02', '英文作者姓名格式',
                         '姓全大写，名缩写。如 STOKES P 非 Stokes, P.', ['J', 'M', 'C', 'D'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        # 检测 "Lastname, F." 格式（应改为 "LASTNAME F"）
        match = re.search(r'([A-Z][a-z]+),\s*([A-Z])\.', text)
        if match:
            wrong = match.group(0)
            correct = f'{match.group(1).upper()} {match.group(2)}'
            return {
                'rule': self.rule_id,
                'error': f'英文作者格式不规范: "{wrong}" → 应为 "{correct}"',
                'suggestion': f'将"{wrong}"改为"{correct}"（姓全大写，名缩写，无逗号）',
                'position': match.start(),
            }
        return None


class Rule03_Separator(Rule):
    """出版地分隔符"""
    def __init__(self):
        super().__init__('r03', '出版地分隔符规范',
                         '中文文献用全角冒号，英文用半角冒号。如"上海: 出版社"非"上海：出版社"',
                         ['J', 'M', 'D', 'R'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        # 检测中文地名后跟全角冒号（应改为半角冒号）
        match = re.search(r'([\u4e00-\u9fff]{2,6})：', text)
        if match:
            if re.search(r'[\u4e00-\u9fff]{2,6}：[\u4e00-\u9fff]', text):
                return {
                    'rule': self.rule_id,
                    'error': f'中文出版地后应使用半角冒号分隔: "{match.group(0)}"',
                    'suggestion': f'将"{match.group(0)}"改为"{match.group(1)}:"',
                    'position': match.start(),
                }
        return None


class Rule04_JournalFormat(Rule):
    """[J]格式完整性"""
    def __init__(self):
        super().__init__('r04', '[J]期刊格式完整性',
                         '必须含：作者.标题[J].期刊名,年,卷(期):页码.', ['J'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        issues = []

        # 检测 . [J] . 格式
        if not re.search(r'\.\s*\[J\]\s*\.', text):
            issues.append('[J]标记格式应为 ". [J] ."')

        # 检测卷(期):页码
        if not re.search(r'\d{4}[,，]', text):
            issues.append('缺少发表年份')
        if not re.search(r'\(\d+\)', text):
            issues.append('缺少期号')
        if not re.search(r'[:：]\s*\d+', text):
            issues.append('缺少页码')

        if issues:
            return {
                'rule': self.rule_id,
                'error': f'[J]格式不完整: {"; ".join(issues)}',
                'suggestion': '完整格式: 作者.标题[J].期刊名,年,卷(期):页码.',
                'position': 0,
            }
        return None


class Rule05_MonographFormat(Rule):
    """[M]格式完整性"""
    def __init__(self):
        super().__init__('r05', '[M]专著格式完整性',
                         '必须含：作者.书名[M].出版地:出版社,年.', ['M'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        issues = []

        if not re.search(r'\.\s*\[M\]\s*\.', text):
            issues.append('[M]标记格式应为 ". [M] ."')
        if not re.search(r'出版|Press|出版社', text):
            issues.append('缺少出版社信息')
        if not re.search(r'\d{4}', text):
            issues.append('缺少出版年份')

        if issues:
            return {
                'rule': self.rule_id,
                'error': f'[M]格式不完整: {"; ".join(issues)}',
                'suggestion': '完整格式: 作者.书名[M].出版地:出版社,年.',
                'position': 0,
            }
        return None


class Rule06_ConferenceFormat(Rule):
    """[C]//格式检测"""
    def __init__(self):
        super().__init__('r06', '[C]//格式规范',
                         '会议论文必须用[C]//格式，禁止APA风格"In...(Eds.)"', ['C'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']

        # 检测APA风格
        if re.search(r'\bIn\b.*\bEds?\.?\b', text, re.IGNORECASE):
            return {
                'rule': self.rule_id,
                'error': '检测到APA格式"In... (Eds.)"，应改用GB/T 7714的[C]//格式',
                'suggestion': '改为: 作者.标题[C]//编者.论文集名.出版地:出版社,年:页码.',
                'position': 0,
            }

        if not re.search(r'\[C\]\s*//', text):
            return {
                'rule': self.rule_id,
                'error': '会议论文应使用[C]//格式',
                'suggestion': '格式: 作者.标题[C]//编者.论文集名.出版地:出版社,年:页码.',
                'position': 0,
            }
        return None


class Rule07_DissertationFormat(Rule):
    """[D]分隔符"""
    def __init__(self):
        super().__init__('r07', '[D]学位论文格式',
                         '分隔符用半角冒号：如"上海: 上海交通大学"非"上海：上海交通大学"', ['D'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']

        if not re.search(r'\.\s*\[D\]\s*\.', text):
            return {
                'rule': self.rule_id,
                'error': '[D]标记格式不正确',
                'suggestion': '完整格式: 作者.标题[D].出版地:学校,年.',
                'position': 0,
            }
        return None


class Rule08_EBOLThreeElements(Rule):
    """[EB/OL]三要素"""
    def __init__(self):
        super().__init__('r08', '[EB/OL]三要素完整性',
                         '必须含：发布日期 + 引用日期 + URL，三者缺一不可', ['EB_OL'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        issues = []

        # 发布日期
        if not re.search(r'\(\d{4}[-\/年]\d{1,2}[-\/月]\d{1,2}[日]?\)', text):
            issues.append('缺少发布日期（格式: (YYYY-MM-DD)）')

        # 引用日期
        if not re.search(r'\[\d{4}[-\/年]\d{1,2}[-\/月]\d{1,2}[日]?\]', text):
            issues.append('缺少引用日期（格式: [YYYY-MM-DD]）')

        # URL
        if not re.search(r'https?://|www\.', text):
            issues.append('缺少URL')
        else:
            url_match = re.search(r'(https?://[^\s.,;]+|www\.[^\s.,;]+)', text)
            if url_match:
                parsed = urlparse(url_match.group(0))
                if not parsed.scheme:
                    issues.append('URL缺少协议头 (http:// 或 https://)')

        if issues:
            return {
                'rule': self.rule_id,
                'error': f'[EB/OL]三要素不完整: {"; ".join(issues)}',
                'suggestion': '完整格式: 作者.标题[EB/OL].(发布日期)[引用日期]. URL.',
                'position': 0,
            }
        return None


class Rule09_EnglishTitleCase(Rule):
    """英文标题sentence case"""
    def __init__(self):
        super().__init__('r09', '英文标题sentence case',
                         '仅首词和专有名词大写', ['J', 'M', 'C', 'D', 'R'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']

        # 检测英文标题区域（在标记之前的英文文本）
        title_match = re.search(r'([A-Z][a-z]+\s+){2,}', text)
        if title_match:
            title = title_match.group(0).strip()
            words = title.split()
            capital_words = [w for w in words[1:] if w[0].isupper() and w.lower() not in
                           {'a', 'an', 'the', 'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'but'}]
            if len(capital_words) >= 2:
                return {
                    'rule': self.rule_id,
                    'error': f'英文标题可能存在过多首字母大写（建议仅首词和专有名词大写）',
                    'suggestion': '检查标题"{title[:60]}..."中是否正确使用了sentence case',
                    'position': title_match.start(),
                }
        return None


class Rule10_ReportFormat(Rule):
    """[R]报告格式"""
    def __init__(self):
        super().__init__('r10', '[R]报告格式完整性',
                         '机构.标题[R].出版地:出版社,年.', ['R'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        if not re.search(r'\.\s*\[R\]\s*\.', text):
            return {
                'rule': self.rule_id,
                'error': '[R]标记格式不正确',
                'suggestion': '完整格式: 机构.标题[R].出版地:出版社,年.',
                'position': 0,
            }
        return None


class Rule11_AuthorSeparator(Rule):
    """作者间分隔符"""
    def __init__(self):
        super().__init__('r11', '作者间分隔符',
                         '中文作者用"，"分隔，英文作者用","分隔', ['J', 'M', 'C', 'D', 'R'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        first_part = text.split('.')[0] if '.' in text else text[:100]

        # 中文逗号后的作者（应用中文逗号）
        if re.search(r'[\u4e00-\u9fff],[^\w\s]', first_part):
            return {
                'rule': self.rule_id,
                'error': '中文作者之间应使用全角逗号"，"而非半角逗号","',
                'suggestion': '将作者间的半角逗号","改为全角逗号"，"',
                'position': 0,
            }
        return None


class Rule12_PageRange(Rule):
    """页码范围检测"""
    def __init__(self):
        super().__init__('r12', '页码范围格式',
                         '页码范围完整，如"123-125"非"123-25"', ['J', 'C'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']

        # 检测页码 "-" 前后的数字
        match = re.search(r'[:：]\s*(\d+)[-‐](\d+)', text)
        if match:
            start_page = match.group(1)
            end_page = match.group(2)
            if len(end_page) < len(start_page) and int(end_page) < int(start_page[-len(end_page):]):
                return {
                    'rule': self.rule_id,
                    'error': f'页码范围可能不完整: {start_page}-{end_page}',
                    'suggestion': f'确认终止页码是否正确（终止页码数位不应少于起始页码）',
                    'position': match.start(),
                }
        return None


class Rule13_DOIField(Rule):
    """DOI字段建议"""
    def __init__(self):
        super().__init__('r13', 'DOI字段建议',
                         '建议含DOI，非强制', ['J', 'C'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        if not re.search(r'(doi|DOI|10\.\d{4,}/)', text):
            return {
                'rule': self.rule_id,
                'error': '建议补充DOI',
                'suggestion': '如有DOI，格式: DOI:10.xxxx/xxxxx',
                'position': len(text),
                'severity': 'suggestion',  # 非强制
            }
        return None


class Rule14_DuplicateDetection(Rule):
    """重复条目检测"""
    def __init__(self):
        super().__init__('r14', '重复条目检测',
                         '同一作者+年份组合不应在不同类型中重复出现', ['J', 'M', 'C', 'D'])

    # 这个规则需要在整体层面检测，单条无法检测
    def check(self, ref: dict) -> Optional[dict]:
        return None  # 由 validate_all 中的全局检查处理


class Rule15_YearRange(Rule):
    """年份范围合理性"""
    def __init__(self):
        super().__init__('r15', '年份范围合理性',
                         '参考文献发表年份一般不超过提交年份减10年（陈旧文献）', ['J', 'M', 'C', 'D', 'R'])

    def check(self, ref: dict) -> Optional[dict]:
        text = ref['text']
        year_match = re.search(r'(?:^|[.,;])\s*(\d{4})\s*[,.;]', text)
        if year_match:
            year = int(year_match.group(1))
            current_year = 2026  # 可配置
            if year < current_year - 15:
                return {
                    'rule': self.rule_id,
                    'error': f'文献年份({year})距今{current_year - year}年，可能过于陈旧',
                    'suggestion': f'建议优先引用近10年内的文献（可保留经典文献，但占比不宜过高）',
                    'position': year_match.start(),
                    'severity': 'suggestion',
                }
        return None


# ============================================================
# GB/T 7714 校验器主类
# ============================================================
class GBT7714Validator:
    """
    GB/T 7714-2015 参考文献格式自动校验器

    支持15条规则，每条可独立启用/禁用。
    """

    def __init__(self, config: dict = None):
        self.config = config or {}

        rule_config = self.config.get('gbt7714', {}).get('rules', {})

        self.rules = []
        rule_classes = [
            Rule01_PeriodNumber, Rule02_EnglishAuthor, Rule03_Separator,
            Rule04_JournalFormat, Rule05_MonographFormat, Rule06_ConferenceFormat,
            Rule07_DissertationFormat, Rule08_EBOLThreeElements, Rule09_EnglishTitleCase,
            Rule10_ReportFormat, Rule11_AuthorSeparator, Rule12_PageRange,
            Rule13_DOIField, Rule14_DuplicateDetection, Rule15_YearRange,
        ]

        for rule_cls in rule_classes:
            instance = rule_cls()
            if rule_config.get(instance.rule_id, True):
                self.rules.append(instance)

    def validate_all(self, manifest: list) -> list:
        """
        对所有文档的参考文献进行校验

        Args:
            manifest: batch_extract.py 生成的文件清单

        Returns:
            List[dict]: 问题列表，每条包含 {file, rule, error, suggestion, position, ref_text}
        """
        all_issues = []

        for item in manifest:
            txt_path = item.get('txt_path', '')
            if not txt_path or not os.path.exists(txt_path):
                continue

            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            refs = split_references(content)
            if not refs:
                continue

            # 逐条参考文献、逐条规则检查
            for ref in refs:
                ref_type = identify_ref_type(ref['text'])

                for rule in self.rules:
                    if isinstance(rule, Rule14_DuplicateDetection):
                        continue  # 跳过，全局处理

                    if ref_type not in rule.applicable_types and 'all' not in getattr(rule, 'applicable_types', []):
                        continue

                    try:
                        result = rule.check(ref)
                        if result:
                            all_issues.append({
                                'file': item['file'],
                                'issue': f'[#{ref["number"]}][{rule.rule_id}] {result["error"]}',
                                'evidence': ref['text'][:100],
                                'confidence': '高',
                                'suggestion': result.get('suggestion', ''),
                                'severity': result.get('severity', 'error'),
                                'rule_id': rule.rule_id,
                                'rule_name': rule.name,
                                'ref_number': ref['number'],
                                'ref_text': ref['text'],
                            })
                    except Exception as e:
                        all_issues.append({
                            'file': item['file'],
                            'issue': f'[#{ref["number"]}] 校验异常: {str(e)}',
                            'evidence': ref['text'][:50],
                            'confidence': '低',
                        })

            # 全局重复检测 (Rule14)
            if self.config.get('gbt7714', {}).get('rules', {}).get('r14', True):
                dup_issues = self._check_duplicates(refs, item['file'])
                all_issues.extend(dup_issues)

        # 按严重程度排序
        all_issues.sort(key=lambda x: (0 if x.get('severity', 'error') == 'error' else 1, x.get('ref_number', 0)))

        return all_issues

    def _check_duplicates(self, refs: list, filename: str) -> list:
        """检测参考文献中的重复条目"""
        issues = []
        keys = {}

        for ref in refs:
            # 提取第一作者+年份作为指纹
            key_match = re.match(r'([A-Z\u4e00-\u9fff]+).*?(\d{4})', ref['text'])
            if key_match:
                key = f'{key_match.group(1)}_{key_match.group(2)}'
                if key in keys:
                    issues.append({
                        'file': filename,
                        'issue': f'重复条目: #[{keys[key]}] 与 #[{ref["number"]}] 疑似重复',
                        'evidence': f'#{keys[key]}: {refs[keys[key]-1]["text"][:50] if keys[key]-1 < len(refs) else "?"}',
                        'confidence': '中',
                        'rule_id': 'r14',
                        'rule_name': '重复条目检测',
                        'ref_number': ref['number'],
                        'severity': 'warning',
                    })
                keys[key] = ref['number']

        return issues

    def validate_text(self, refs_text: str, filename: str = 'references') -> list:
        """直接校验参考文献文本（无需 manifest）"""
        refs = split_references(refs_text)
        if not refs:
            return []

        all_issues = []

        for ref in refs:
            ref_type = identify_ref_type(ref['text'])

            for rule in self.rules:
                if isinstance(rule, Rule14_DuplicateDetection):
                    continue
                if ref_type not in rule.applicable_types:
                    continue

                try:
                    result = rule.check(ref)
                    if result:
                        all_issues.append({
                            'file': filename,
                            'issue': f'[#{ref["number"]}][{rule.rule_id}] {result["error"]}',
                            'evidence': ref['text'][:100],
                            'confidence': '高',
                            'suggestion': result.get('suggestion', ''),
                            'severity': result.get('severity', 'error'),
                            'rule_name': rule.name,
                            'ref_number': ref['number'],
                            'ref_text': ref['text'],
                        })
                except Exception as e:
                    all_issues.append({
                        'file': filename,
                        'issue': f'[#{ref["number"]}] 校验异常: {str(e)}',
                        'evidence': ref['text'][:50],
                        'confidence': '低',
                    })

        dup_issues = self._check_duplicates(refs, filename)
        all_issues.extend(dup_issues)

        all_issues.sort(key=lambda x: (0 if x.get('severity', 'error') == 'error' else 1, x.get('ref_number', 0)))

        return all_issues


# ============================================================
# 独立运行支持
# ============================================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description='GB/T 7714-2015 参考文献格式校验器')
    parser.add_argument('--input', '-i', help='参考文献文本文件路径')
    parser.add_argument('--txt-dir', help='提取文本目录（批量校验）')
    parser.add_argument('--output', '-o', help='输出JSON路径')
    parser.add_argument('--rules', help='启用的规则（逗号分隔，如 r01,r02,r04），默认全部启用')
    args = parser.parse_args()

    # 配置规则
    enabled_rules = {f'r{i:02d}': True for i in range(1, 16)}
    if args.rules:
        enabled_rules = {f'r{i:02d}': False for i in range(1, 16)}
        for r in args.rules.split(','):
            r = r.strip()
            if r in enabled_rules:
                enabled_rules[r] = True

    config = {'gbt7714': {'rules': enabled_rules}}
    validator = GBT7714Validator(config)

    all_issues = []

    if args.input:
        with open(args.input, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
        all_issues = validator.validate_text(text, os.path.basename(args.input))

    elif args.txt_dir:
        manifest_path = os.path.join(args.txt_dir, '_manifest.json')
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            all_issues = validator.validate_all(manifest)
        else:
            print(f'[ERROR] manifest 不存在: {manifest_path}')
            return
    else:
        parser.print_help()
        return

    # 输出
    output = {
        'total_issues': len(all_issues),
        'error_count': sum(1 for i in all_issues if i.get('severity', 'error') == 'error'),
        'suggestion_count': sum(1 for i in all_issues if i.get('severity') == 'suggestion'),
        'issues': all_issues,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        print(f'\n校验结果已保存至: {args.output}')

    print(f'\n共发现 {len(all_issues)} 条参考文献格式问题')
    print(f'  错误(errors): {output["error_count"]} 条')
    print(f'  建议(suggestions): {output["suggestion_count"]} 条')


if __name__ == '__main__':
    main()
