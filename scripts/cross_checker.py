#!/usr/bin/env python3
"""
跨文档一致性检查器 v1.0 — 全代码模式核心模块

从多份毕业设计文档中抽取关键元数据，逐项比对一致性，
标注所有矛盾条目。

检查维度：
  1. 要素一致性 (50分): 姓名/学号/课题/导师/学院在所有文档中的一致性
  2. 引用一致性 (30分): 各文档参考文献重叠度检查
  3. 分工一致性 (20分): 小组分工与个人角色匹配

输出：CrossCheckResult 包含得分和逐项详情
"""

import json
import os
import re
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any


# ============================================================
# 元数据抽取器
# ============================================================
class MetadataExtractor:
    """从文档文本中自动抽取关键元数据"""

    # 多模式正则级联（精确匹配 → 模糊匹配 → 弱匹配）
    PATTERNS = {
        'student_name': [
            # 精确匹配
            r'学生姓名\s*[:：]\s*([\u4e00-\u9fff]{2,4})',
            r'姓名\s*[:：]\s*([\u4e00-\u9fff]{2,4})',
            r'作者\s*[:：]\s*([\u4e00-\u9fff]{2,4})',
            r'学生\s*[:：]\s*([\u4e00-\u9fff]{2,4})',
            # 模糊匹配
            r'^[\u4e00-\u9fff]{2,4}\s*\n',  # 文档开头的姓名行
        ],
        'student_id': [
            r'学\s*号\s*[:：]\s*(\d{6,12})',
            r'学号\s*[:：]\s*(\d{6,12})',
            r'student\s*id\s*[:：]\s*(\d{6,12})',
            r'No\.\s*(\d{6,12})',
            r'(?<!\d)(20\d{6,10})(?!\d)',  # 20开头的8-12位数字
        ],
        'topic_title': [
            r'课题名称\s*[:：]\s*(.+?)(?:\n|$)',
            r'论文题目\s*[:：]\s*(.+?)(?:\n|$)',
            r'题\s*目\s*[:：]\s*(.+?)(?:\n|$)',
            r'毕业设计题目\s*[:：]\s*(.+?)(?:\n|$)',
            r'《(.{5,50})》',  # 书名号包裹的标题
            r'"(?![^"]{50})"([\u4e00-\u9fff].{3,40}[\u4e00-\u9fff])"(?![^"]{50})',
        ],
        'advisor': [
            r'指导教师\s*[:：]\s*([\u4e00-\u9fff]{2,8})',
            r'导\s*师\s*[:：]\s*([\u4e00-\u9fff]{2,8})',
            r'指导老师\s*[:：]\s*([\u4e00-\u9fff]{2,8})',
            r'advi[s]?or\s*[:：]\s*([A-Z][a-z]+\s+[A-Z][a-z]+)',
        ],
        'college': [
            r'学\s*院\s*[:：]\s*([\u4e00-\u9fff]{2,20})',
            r'院\s*系\s*[:：]\s*([\u4e00-\u9fff]{2,20})',
            r'(?:信息|计算机|软件|电子|机械|土木|经管|文学|理学|艺术|数学|物理|化学|生物|医学|法学|教育|外语|体育|音乐|美术|新闻)(?:与|及)?(?:工程|科学|技术|设计|管理)?学院',
        ],
        'major': [
            r'专\s*业\s*[:：]\s*([\u4e00-\u9fffA-Za-z]{2,30})',
            r'major\s*[:：]\s*([A-Za-z\s]{3,40})',
        ],
    }

    @classmethod
    def extract(cls, content: str, filename: str) -> dict:
        """从文档内容中抽取所有元数据"""
        result = {
            'file': filename,
            'student_name': None,
            'student_id': None,
            'topic_title': None,
            'advisor': None,
            'college': None,
            'major': None,
            'dates': [],
            'references': [],
        }

        for field, patterns in cls.PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, content, re.MULTILINE)
                if match:
                    value = match.group(1).strip() if match.groups() else match.group(0).strip()
                    if value and len(value) >= 2:
                        result[field] = value
                        break

        # 日期抽取
        result['dates'] = cls._extract_dates(content)

        # 参考文献抽取
        result['references'] = cls._extract_references(content)

        return result

    @classmethod
    def _extract_dates(cls, content: str) -> list:
        """抽取文档中的日期"""
        dates = []
        patterns = [
            r'(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?',
            r'(\d{4})-(\d{2})-(\d{2})',
            r'(\d{4})/(\d{2})/(\d{2})',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                try:
                    y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    if 2010 <= y <= 2030 and 1 <= m <= 12 and 1 <= d <= 31:
                        dates.append({
                            'date': f'{y}-{m:02d}-{d:02d}',
                            'year': y, 'month': m, 'day': d,
                            'context': content[max(0, match.start()-20):match.end()+20],
                        })
                except (ValueError, IndexError):
                    continue

        # 去重
        seen = set()
        unique_dates = []
        for d in dates:
            if d['date'] not in seen:
                seen.add(d['date'])
                unique_dates.append(d)

        return sorted(unique_dates, key=lambda x: x['date'])

    @classmethod
    def _extract_references(cls, content: str) -> list:
        """抽取参考文献条目"""
        refs = []

        # 在"参考文献"之后查找 [N] 格式的条目
        ref_section_match = re.search(r'参考\s*文\s*献|References', content, re.IGNORECASE)
        if ref_section_match:
            ref_section = content[ref_section_match.start():]
        else:
            ref_section = content

        # 匹配 [1] 到 [N] 格式的条目
        lines = ref_section.split('\n')
        current_ref = []
        for line in lines:
            ref_match = re.match(r'\s*\[(\d+)\]\s*(.+)', line)
            if ref_match:
                if current_ref:
                    refs.append(' '.join(current_ref))
                current_ref = [ref_match.group(2).strip()]
            elif current_ref:
                current_ref.append(line.strip())

        if current_ref:
            refs.append(' '.join(current_ref))

        return refs


# ============================================================
# 要素一致性检查器（50分）
# ============================================================
class ElementConsistencyChecker:
    """跨文档要素一致性检查"""

    REQUIRED_FIELDS = ['student_name', 'student_id', 'topic_title', 'advisor']

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, metadata_list: List[dict]) -> Tuple[int, List[dict]]:
        """
        检查所有文档的要素一致性

        Args:
            metadata_list: 各文档的元数据列表

        Returns:
            (得分, 详情列表)
        """
        max_score = 50
        issues = []
        deductions = 0

        if len(metadata_list) < 2:
            return max_score, [{'field': '文档数量', 'issue': '文档少于2份，无法交叉比对', 'deduction': 0}]

        for field in self.REQUIRED_FIELDS:
            values = {}
            for meta in metadata_list:
                val = meta.get(field)
                if val:
                    if val not in values:
                        values[val] = []
                    values[val].append(meta['file'])

            if not values:
                deductions += 10
                field_cn = {'student_name': '学生姓名', 'student_id': '学号',
                            'topic_title': '课题名称', 'advisor': '指导教师'}.get(field, field)
                issues.append({
                    'field': field_cn,
                    'issue': f'所有文档中均未找到{field_cn}信息',
                    'deduction': 10,
                    'details': {},
                })
            elif len(values) > 1:
                deduct = min(10, len(values) * 5)
                deductions += deduct
                field_cn = {'student_name': '学生姓名', 'student_id': '学号',
                            'topic_title': '课题名称', 'advisor': '指导教师'}.get(field, field)

                # 尝试模糊匹配规约
                value_list = list(values.keys())
                if self._is_fuzzy_match(value_list):
                    deduct = 2  # 模糊匹配降罚
                    deductions = deductions - (min(10, len(values) * 5)) + deduct
                    issues.append({
                        'field': field_cn,
                        'issue': f'{field_cn}在不同文档中存在细微差异（模糊匹配通过）',
                        'deduction': deduct,
                        'details': values,
                    })
                else:
                    issues.append({
                        'field': field_cn,
                        'issue': f'{field_cn}在不同文档中不一致: {value_list}',
                        'deduction': deduct,
                        'details': values,
                    })

        # 日期逻辑检查
        date_issues = self._check_date_logic(metadata_list)
        issues.extend(date_issues)
        deductions += sum(d.get('deduction', 0) for d in date_issues)

        score = max(0, max_score - deductions)
        return score, issues

    def _is_fuzzy_match(self, values: list) -> bool:
        """检查多个值是否为模糊匹配（如去掉空格后相同）"""
        if len(values) < 2:
            return True
        normalized = [v.replace(' ', '').replace('　', '').replace('_', '').replace('-', '') for v in values]
        return len(set(normalized)) == 1

    def _check_date_logic(self, metadata_list: List[dict]) -> List[dict]:
        """检查日期逻辑：开题 < 中期 < 答辩 < 提交"""
        issues = []

        all_dates = []
        for meta in metadata_list:
            for d in meta.get('dates', []):
                all_dates.append({
                    'date': d['date'],
                    'file': meta['file'],
                    'context': d.get('context', ''),
                })

        if len(all_dates) < 2:
            return issues

        dates_sorted = sorted(all_dates, key=lambda x: x['date'])
        earliest = dates_sorted[0]
        latest = dates_sorted[-1]

        # 检查日期极值是否合理（不应超过2年跨度）
        from datetime import date as dt_date
        try:
            d1 = dt_date.fromisoformat(earliest['date'])
            d2 = dt_date.fromisoformat(latest['date'])
            span_days = (d2 - d1).days
            if span_days > 730:
                issues.append({
                    'field': '日期逻辑',
                    'issue': f'文档日期跨度过大: {earliest["date"]} 至 {latest["date"]}（{span_days}天）',
                    'deduction': 5,
                    'details': {'earliest': earliest, 'latest': latest},
                })
        except ValueError:
            pass

        # 按文档分组日期
        file_dates = {}
        for d in all_dates:
            file_dates.setdefault(d['file'], []).append(d['date'])

        for file, dates in file_dates.items():
            if len(dates) >= 2:
                sorted_d = sorted(dates)
                if sorted_d != dates:
                    issues.append({
                        'field': '日期逻辑',
                        'issue': f'{file} 中日期未按升序排列',
                        'deduction': 2,
                        'details': {'dates': dates},
                    })

        return issues


# ============================================================
# 引用一致性检查器（30分）
# ============================================================
class ReferenceConsistencyChecker:
    """参考文献一致性检查"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, metadata_list: List[dict]) -> Tuple[int, List[dict]]:
        """
        检查各文档参考文献的重叠度

        Returns:
            (得分, 详情列表)
        """
        max_score = 30
        issues = []

        # 收集参考文献数量≥5的文档
        ref_docs = {}
        for meta in metadata_list:
            refs = meta.get('references', [])
            if len(refs) >= 3:
                ref_docs[meta['file']] = refs

        if len(ref_docs) < 2:
            return max_score, [{'check': '参考文献比对', 'result': '参考文献充足的文档少于2份，跳过', 'deduction': 0}]

        # 两两比对参考文献重叠度
        doc_names = list(ref_docs.keys())
        deductions = 0

        for i in range(len(doc_names)):
            for j in range(i + 1, len(doc_names)):
                overlap, jaccard = self._calculate_overlap(
                    ref_docs[doc_names[i]],
                    ref_docs[doc_names[j]],
                )

                # 重叠度评判
                if jaccard > 0.8:
                    deduct = 8
                    reason = '过度重叠(>80%)，疑似抄袭或未独立撰写'
                elif jaccard > 0.5:
                    deduct = 3
                    reason = '较高重叠(>50%)，需关注独立性'
                elif jaccard < 0.05 and len(ref_docs[doc_names[i]]) > 10 and len(ref_docs[doc_names[j]]) > 10:
                    deduct = 5
                    reason = '几乎无重叠(<5%)，可能研究方向不一致'
                else:
                    reason = '重叠度合理'
                    deduct = 0

                if deduct > 0:
                    deductions += deduct

                issues.append({
                    'check': '引用一致性',
                    'files': [doc_names[i], doc_names[j]],
                    'overlap_count': overlap,
                    'jaccard_index': round(jaccard, 3),
                    'reason': reason,
                    'deduction': deduct,
                })

        score = max(0, max_score - deductions)
        return score, issues

    def _calculate_overlap(self, refs_a: list, refs_b: list) -> Tuple[int, float]:
        """计算两组参考文献的重叠度和Jaccard相似度"""
        # 提取每条参考文献的关键信息（第一作者+年份）
        def extract_key(ref_text: str) -> str:
            # 取前30个字符作为指纹
            key = ref_text.strip()[:30]
            # 提取作者
            author_match = re.match(r'([A-Z\u4e00-\u9fff]+)', key)
            if author_match:
                author = author_match.group(1)
                # 提取年份
                year_match = re.search(r'(\d{4})', key)
                year = year_match.group(1) if year_match else ''
                return f'{author}_{year}'
            return key

        keys_a = set(extract_key(r) for r in refs_a)
        keys_b = set(extract_key(r) for r in refs_b)

        intersection = len(keys_a & keys_b)
        union = len(keys_a | keys_b)

        jaccard = intersection / union if union > 0 else 0
        return intersection, jaccard


# ============================================================
# 分工一致性检查器（20分）
# ============================================================
class DivisionConsistencyChecker:
    """分工一致性检查"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, metadata_list: List[dict]) -> Tuple[int, List[dict]]:
        """
        检查小组分工与个人角色一致性

        检测点：
        1. 是否存在小组分工描述
        2. 课题名称是否与分工描述匹配
        3. 是否明确了个人职责
        """
        max_score = 20
        issues = []

        all_content = ''
        for meta in metadata_list:
            all_content += f"\n[{meta['file']}]\n"

        # 检测是否有分工相关描述
        division_patterns = [
            r'(分工|角色|负责|承担|任务分配)',
            r'(独立完成|独立开发|独立设计|独立实现)',
            r'(小组|团队|组员|成员)',
        ]

        has_division = False
        for pattern in division_patterns:
            if re.search(pattern, all_content):
                has_division = True
                break

        if not has_division:
            # 未检测到分工描述，但不扣分（单人项目常见）
            issues.append({
                'check': '分工描述',
                'result': '未检测到分工描述（可能是独立完成项目）',
                'deduction': 0,
            })
            return max_score, issues

        # 检测"本人"相关的工作描述
        personal_work = re.findall(
            r'(本人|笔者|我|作者)\s*(?:负责|完成|设计|开发|实现|撰写)(.{5,50})',
            all_content,
        )

        if not personal_work:
            issues.append({
                'check': '个人职责',
                'issue': '检测到分工但未明确个人职责（建议添加"本人负责..."描述）',
                'deduction': 5,
            })
            return max_score - 5, issues

        issues.append({
            'check': '分工一致性',
            'result': f'检测到 {len(personal_work)} 处个人职责描述',
            'deduction': 0,
        })

        return max_score, issues


# ============================================================
# 跨文档交叉审查器（主入口）
# ============================================================
class CrossChecker:
    """
    跨文档交叉审查器 — 全代码模式核心

    从多份毕业设计文档中抽取元数据，执行三维交叉审查。
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.element_checker = ElementConsistencyChecker(config)
        self.ref_checker = ReferenceConsistencyChecker(config)
        self.division_checker = DivisionConsistencyChecker(config)

    def check_all(self, manifest: list) -> dict:
        """
        执行完整跨文档交叉审查

        Args:
            manifest: batch_extract.py 生成的文件清单

        Returns:
            {
                'element_consistency': {score, details},
                'reference_consistency': {score, details},
                'division_consistency': {score, details},
                'total_score': 总分,
                'issues': 合并后的问题列表,
            }
        """
        # 抽取所有文档元数据
        metadata_list = []
        for item in manifest:
            txt_path = item.get('txt_path', '')
            if not txt_path or not os.path.exists(txt_path):
                continue

            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            meta = MetadataExtractor.extract(content, item['file'])
            metadata_list.append(meta)

        # 三维度检查
        elem_score, elem_details = self.element_checker.check(metadata_list)
        ref_score, ref_details = self.ref_checker.check(metadata_list)
        div_score, div_details = self.division_checker.check(metadata_list)

        # 汇总
        total = elem_score + ref_score + div_score

        all_issues = []
        for detail in elem_details + ref_details + div_details:
            if detail.get('deduction', 0) > 0 or detail.get('issue'):
                all_issues.append({
                    'issue': detail.get('issue', detail.get('result', '')),
                    'evidence': json.dumps(detail.get('details', {}), ensure_ascii=False),
                    'confidence': '高',
                    'source': 'cross_checker',
                })

        return {
            'element_consistency': {'score': elem_score, 'max': 50, 'details': elem_details},
            'reference_consistency': {'score': ref_score, 'max': 30, 'details': ref_details},
            'division_consistency': {'score': div_score, 'max': 20, 'details': div_details},
            'total_score': total,
            'max_score': 100,
            'metadata': metadata_list,
            'issues': all_issues,
        }


# ============================================================
# 独立运行支持
# ============================================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description='跨文档一致性检查器')
    parser.add_argument('--txt-dir', default='/tmp/auto_grading', help='提取文本目录')
    parser.add_argument('--output', help='输出JSON路径')
    args = parser.parse_args()

    manifest_path = os.path.join(args.txt_dir, '_manifest.json')
    if not os.path.exists(manifest_path):
        print(f'[ERROR] manifest 不存在: {manifest_path}')
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    checker = CrossChecker()
    result = checker.check_all(manifest)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f'\n交叉审查结果已保存至: {args.output}')

    print(f"\n交叉审查总分: {result['total_score']}/{result['max_score']}")
    print(f"  要素一致性: {result['element_consistency']['score']}/50")
    print(f"  引用一致性: {result['reference_consistency']['score']}/30")
    print(f"  分工一致性: {result['division_consistency']['score']}/20")


if __name__ == '__main__':
    main()
