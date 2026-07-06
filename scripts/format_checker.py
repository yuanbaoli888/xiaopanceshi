#!/usr/bin/env python3
"""
格式规范检查器 v1.0 — 全代码模式核心模块

基于规则引擎自动检查文档格式规范性。
覆盖8大检查维度，逐条标注问题出处和建议修正方案。

检查维度：
  1. 图表编号连续性与章节对应
  2. 正文引用"如图X-Y所示"覆盖检测
  3. 域代码残留检测
  4. 模板占位符检测
  5. 页眉页脚页码检查
  6. 审批栏完整性
  7. 诚信承诺书签名检测
  8. 中英文标题一致性

用法:
  python format_checker.py --txt-dir /tmp/auto_grading --output issues.json
"""

import json
import os
import re
import sys
from typing import Dict, List, Tuple, Optional


# ============================================================
# 检查1: 图表编号连续性与章节对应
# ============================================================
class FigureTableNumberChecker:
    """检查图表编号是否连续、是否与所在章节一致"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str) -> List[dict]:
        issues = []

        # 提取所有"图X-Y"和"表X-Y"编号
        fig_matches = re.findall(r'图\s*(\d+)[-—\u3000](\d+)', content)
        table_matches = re.findall(r'表\s*(\d+)[-—\u3000](\d+)', content)

        # 检查图编号连续性
        for label, matches in [('图', fig_matches), ('表', table_matches)]:
            if not matches:
                continue

            # 按章节分组
            by_chapter = {}
            for ch, num in matches:
                ch = int(ch)
                num = int(num)
                by_chapter.setdefault(ch, []).append(num)

            for chapter, numbers in sorted(by_chapter.items()):
                sorted_nums = sorted(numbers)
                unique_nums = sorted(set(sorted_nums))

                # 检查是否从1开始
                if sorted_nums[0] != 1:
                    issues.append({
                        'file': filename,
                        'check': f'{label}编号起始',
                        'issue': f'第{chapter}章{label}编号应从{label}{chapter}-1开始，实际从{label}{chapter}-{sorted_nums[0]}开始',
                        'confidence': '高',
                    })

                duplicate_nums = [num for num in unique_nums if sorted_nums.count(num) > 1]
                if duplicate_nums:
                    shown = [f'{label}{chapter}-{num}' for num in duplicate_nums[:12]]
                    suffix = f' 等，共{len(duplicate_nums)}处' if len(duplicate_nums) > 12 else f'，共{len(duplicate_nums)}处'
                    issues.append({
                        'file': filename,
                        'check': f'{label}编号重复',
                        'issue': f'第{chapter}章{label}编号疑似重复：{", ".join(shown)}{suffix}',
                        'confidence': '高',
                        'count': len(duplicate_nums),
                        'items': shown,
                    })

                # 检查是否连续
                for i in range(len(unique_nums) - 1):
                    if unique_nums[i+1] - unique_nums[i] > 1:
                        missing = list(range(unique_nums[i]+1, unique_nums[i+1]))
                        issues.append({
                            'file': filename,
                            'check': f'{label}编号连续性',
                            'issue': f'{label}{chapter}-{unique_nums[i]} 与 {label}{chapter}-{unique_nums[i+1]} 之间缺少编号: {missing}',
                            'confidence': '中',
                        })

            # 检查章节号最大跳跃（如第1章到第5章，中间章节可能有图但未编号）
            chapters = sorted(by_chapter.keys())
            for i in range(len(chapters) - 1):
                if chapters[i+1] - chapters[i] > 2:
                    issues.append({
                        'file': filename,
                        'check': f'{label}章节分布',
                        'issue': f'{label}编号从第{chapters[i]}章跳至第{chapters[i+1]}章，中间章节可能缺少{label}编号',
                        'confidence': '低',
                    })

        return issues


# ============================================================
# 检查2: 正文引用覆盖检测
# ============================================================
class FigureReferenceChecker:
    """检查正文中"如图X-Y所示"的引用是否覆盖所有图注"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str) -> List[dict]:
        issues = []

        # 提取所有图注（如"图1-1 xxx"）
        figures = re.findall(r'图\s*(\d+)[-—\u3000](\d+)\s+(.{2,60})', content)

        # 提取所有正文引用（如"如图1-1所示"）
        refs_in_text = set()
        for m in re.finditer(r'如图\s*(\d+)[-—\u3000](\d+)', content):
            refs_in_text.add(f'{m.group(1)}-{m.group(2)}')
        for m in re.finditer(r'图\s*(\d+)[-—\u3000](\d+)\s*所示', content):
            refs_in_text.add(f'{m.group(1)}-{m.group(2)}')

        unchecked_figs = []
        for ch, num, caption in figures:
            fig_id = f'{ch}-{num}'
            if fig_id not in refs_in_text:
                unchecked_figs.append(f'图{fig_id} ({caption[:30]}...)')

        if unchecked_figs:
            issues.append({
                'file': filename,
                'check': '图引用覆盖',
                'issue': f'以下{len(unchecked_figs)}个图未在正文中被引用: {", ".join(unchecked_figs[:5])}{"..." if len(unchecked_figs) > 5 else ""}',
                'confidence': '中',
            })

        return issues


# ============================================================
# 检查3: 域代码残留检测
# ============================================================
class FieldCodeChecker:
    """检测Word域代码残留"""

    FIELD_CODES = [
        ('TOC', r'TOC\s', '目录域代码'),
        ('HYPERLINK', r'HYPERLINK\s', '超链接域代码'),
        ('PAGEREF', r'PAGEREF\s', '页码引用域代码'),
        ('REF', r'REF\s_Ref', '交叉引用域代码'),
        ('SEQ', r'SEQ\s', '序列域代码'),
        ('MERGEFORMAT', r'MERGEFORMAT', '格式合并域代码'),
        ('SHAPE', r'SHAPE\s', '图形域代码'),
        ('QUOTE', r'QUOTE\s', '引用域代码'),
    ]

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str, manifest_entry: dict = None) -> List[dict]:
        issues = []

        for code_name, pattern, desc in self.FIELD_CODES:
            matches = re.findall(pattern, content)
            if matches:
                issues.append({
                    'file': filename,
                    'check': '域代码残留',
                    'issue': f'检测到 {len(matches)} 处{desc}残留 ({code_name})',
                    'evidence': matches[0][:60] if matches else '',
                    'confidence': '高',
                    'severity': 'warning',
                })

        return issues


# ============================================================
# 检查4: 模板占位符检测
# ============================================================
class PlaceholderChecker:
    """检测未替换的模板占位符和格式说明文字"""

    SIGNALS = [
        ('高置信度-占位符', [
            '请在此处输入', '请输入', '请更换', '请替换',
            '此处写', '此处输入', '此处填写',
            'XXX', 'xxxx', 'xxxxx',
        ]),
        ('高置信度-格式说明', [
            '删除此句', '删除此行', '删除此段',
            '作品名字', '全小写', '宋体小四', '黑体三号',
            '请直接输入', '格式说明',
            '一级标题用三号', '正文用五号',
        ]),
        ('中置信度-疑似', [
            '占位', '填充', '待补充', '待填写', 'TODO', 'TBD',
            '（修改）', '(修改)', '（请', '(请',
        ]),
    ]

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str) -> List[dict]:
        issues = []

        for conf_level, signals in self.SIGNALS:
            for signal in signals:
                if signal in content:
                    count = content.count(signal)
                    issues.append({
                        'file': filename,
                        'check': '模板残留',
                        'issue': f'检测到{count}处未处理的模板信号: "{signal}"',
                        'confidence': conf_level.split('-')[0],
                    })

        return issues


# ============================================================
# 检查5: 页眉页脚页码检查
# ============================================================
class HeaderFooterChecker:
    """检查页眉页脚页码一致性"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str) -> List[dict]:
        issues = []

        # 检测页眉（通常是重复出现的标题文字）
        header_like = re.findall(r'^(?:毕业设计|毕业论文|本科|硕士|学士).{3,30}$', content, re.MULTILINE)

        # 检测页码
        page_numbers = re.findall(r'[-—]{0,2}\s*(\d{1,3})\s*[-—]{0,2}\s*(?:\n|$)', content)

        if len(page_numbers) >= 2:
            nums = [int(n) for n in page_numbers if n.isdigit()]
            if nums:
                # 检查是否连续
                if min(nums) != 1 and len(nums) > 3:
                    issues.append({
                        'file': filename,
                        'check': '页码起始',
                        'issue': f'页码从{min(nums)}开始而非第1页，可能正文前有独立页码',
                        'confidence': '低',
                    })

                # 检测重复
                seen = set()
                for n in nums:
                    if n in seen:
                        issues.append({
                            'file': filename,
                            'check': '页码重复',
                            'issue': f'页码{n}出现重复',
                            'confidence': '中',
                        })
                        break
                    seen.add(n)

        return issues


# ============================================================
# 检查6: 审批栏完整性
# ============================================================
class ApprovalChecker:
    """检查审批栏（签名/日期/盖章）是否完整"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str) -> List[dict]:
        issues = []

        # 检测签名栏
        sign_section = re.search(r'(指导教师|导师|评阅人|答辩委员会|评审).{0,10}(?:签名|签字|签章)', content)
        if sign_section:
            # 检查签名栏后是否有内容
            after_sign = content[sign_section.end():sign_section.end()+200]
            has_content = len(after_sign.strip()) > 10

            if not has_content:
                issues.append({
                    'file': filename,
                    'check': '审批栏完整',
                    'issue': f'审批栏"{sign_section.group(0)}"后无签名/签章内容',
                    'confidence': '高',
                })

        # 检测日期栏
        date_fields = re.findall(r'(年\s*月\s*日|日\s*期\s*[:：])', content)
        empty_dates = 0
        for m in re.finditer(r'(年\s*月\s*日|日期\s*[:：])', content):
            after = content[m.end():m.end()+20]
            if not re.search(r'\d', after):
                empty_dates += 1

        if empty_dates > 0:
            issues.append({
                'file': filename,
                'check': '审批栏完整',
                'issue': f'检测到 {empty_dates} 处日期栏未填写',
                'confidence': '高',
            })

        return issues


# ============================================================
# 检查7: 诚信承诺书签名
# ============================================================
class IntegrityChecker:
    """检查诚信承诺书是否已签名"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str) -> List[dict]:
        issues = []

        # 检测诚信承诺书
        integrity_match = re.search(
            r'(诚信|学术诚信|学术道德|原创性|独创性|知识产权).{0,20}(?:声明|承诺|保证|责任)',
            content,
        )

        if integrity_match:
            after_decl = content[integrity_match.end():integrity_match.end()+500]

            # 检测签名
            has_signature = bool(re.search(r'(签名|签字|签章|署名|作者签名)', after_decl))
            has_date = bool(re.search(r'\d{4}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2}', after_decl))

            if has_signature and not has_date:
                issues.append({
                    'file': filename,
                    'check': '诚信承诺书',
                    'issue': '诚信承诺书有签名栏但未检测到日期',
                    'confidence': '高',
                })
            elif not has_signature:
                issues.append({
                    'file': filename,
                    'check': '诚信承诺书',
                    'issue': '诚信承诺书未检测到签名',
                    'confidence': '中',
                })

        return issues


# ============================================================
# 检查8: 中英文标题一致性
# ============================================================
class TitleConsistencyChecker:
    """检查中文标题与英文标题是否一致"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def check(self, content: str, filename: str) -> List[dict]:
        issues = []

        # 提取中文标题
        cn_title_match = re.search(
            r'(?:题目|标题|论文题目|课题名称)\s*[:：]\s*([\u4e00-\u9fff《》\w\s\-——]{5,60})',
            content,
        )

        # 提取英文标题
        en_title_match = re.search(
            r'(?:Title|Subject|Topic)\s*[:：]\s*([A-Z][A-Za-z\s:\-]{10,120})',
            content,
        )

        if cn_title_match and not en_title_match:
            issues.append({
                'file': filename,
                'check': '英文标题',
                'issue': '有中文标题但未找到对应英文标题',
                'confidence': '低',
            })
        elif en_title_match and not cn_title_match:
            issues.append({
                'file': filename,
                'check': '中文标题',
                'issue': '有英文标题但未找到对应中文标题',
                'confidence': '低',
            })

        # 也检测第一页的标题
        first_title = re.search(r'^[\u4e00-\u9fff《》].{5,60}$', content[:500], re.MULTILINE)
        first_en = re.search(r'^[A-Z][A-Za-z\s:\-]{10,120}$', content[:500], re.MULTILINE)

        if first_title and not first_en and len(content) > 3000:
            issues.append({
                'file': filename,
                'check': '英文标题',
                'issue': '文档有中文标题但无英文标题（建议国际化学位论文补充英文标题）',
                'confidence': '低',
            })

        return issues


# ============================================================
# 格式规范检查器（主入口）
# ============================================================
class FormatChecker:
    """
    格式规范检查器 — 全代码模式核心

    对单份文档执行8大维度的格式规范性检查。
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.checkers = [
            FigureTableNumberChecker(config),
            FigureReferenceChecker(config),
            FieldCodeChecker(config),
            PlaceholderChecker(config),
            HeaderFooterChecker(config),
            ApprovalChecker(config),
            IntegrityChecker(config),
            TitleConsistencyChecker(config),
        ]
        # 可通过配置禁用某些检查
        self.enabled = config.get('format_check', {}).get('enabled_checks', None)

    def check_single(self, filename: str, content: str,
                      manifest_entry: dict = None) -> List[dict]:
        """对单个文档执行全部格式检查"""
        all_issues = []

        for checker in self.checkers:
            checker_name = checker.__class__.__name__
            if self.enabled and checker_name not in self.enabled:
                continue

            try:
                # 部分检查器需要 manifest_entry
                if isinstance(checker, FieldCodeChecker):
                    issues = checker.check(content, filename, manifest_entry)
                else:
                    issues = checker.check(content, filename)
                all_issues.extend(issues)
            except Exception as e:
                all_issues.append({
                    'file': filename,
                    'check': checker_name,
                    'issue': f'检查异常: {str(e)}',
                    'confidence': '低',
                })

        return all_issues

    def check_all(self, manifest: list) -> list:
        """
        对所有文档执行格式检查

        Args:
            manifest: batch_extract.py 生成的文件清单

        Returns:
            List[dict]: 问题列表
        """
        all_issues = []

        for item in manifest:
            txt_path = item.get('txt_path', '')
            if not txt_path or not os.path.exists(txt_path):
                continue

            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            filename = item['file']
            issues = self.check_single(filename, content, item)
            all_issues.extend(issues)

        return all_issues


# ============================================================
# 独立运行支持
# ============================================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description='格式规范检查器')
    parser.add_argument('--txt-dir', default='/tmp/auto_grading', help='提取文本目录')
    parser.add_argument('--output', help='输出JSON路径')
    parser.add_argument('--file', help='检查单个文件')
    args = parser.parse_args()

    checker = FormatChecker()
    all_issues = []

    if args.file:
        with open(args.file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        all_issues = checker.check_single(os.path.basename(args.file), content)
    else:
        manifest_path = os.path.join(args.txt_dir, '_manifest.json')
        if not os.path.exists(manifest_path):
            print(f'[ERROR] manifest 不存在: {manifest_path}')
            return
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        all_issues = checker.check_all(manifest)

    # 分类统计
    by_check = {}
    for issue in all_issues:
        ck = issue.get('check', 'other')
        by_check.setdefault(ck, []).append(issue)

    output = {
        'total_issues': len(all_issues),
        'by_category': {k: len(v) for k, v in by_check.items()},
        'issues': all_issues,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        print(f'\n结果已保存至: {args.output}')

    print(f'\n共发现 {len(all_issues)} 条格式问题')
    for cat, count in sorted(output['by_category'].items()):
        print(f'  {cat}: {count}条')


if __name__ == '__main__':
    main()
