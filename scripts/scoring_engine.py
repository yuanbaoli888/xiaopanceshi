#!/usr/bin/env python3
"""
评分计算引擎 v1.0 — 全代码模式核心模块

基于规则引擎对毕业设计各文档进行多维度自动评分。
替代 LLM 评审，实现确定性、可复现的评分逻辑。

评分维度：
  创作报告/文献综述/开题报告: 内容完整性(40) + 格式规范性(20) + 逻辑一致性(20) + 论证合理性(20)
  外文翻译: 格式规范性(30) + 翻译质量(30)
  申报表/任务书: 格式规范性(20) + 栏位完整性(20)
  跨文档交叉审查: 要素一致性(50) + 引用一致性(30) + 分工一致性(20)
  PPT答辩: 基本信息(20) + 内容结构(30) + 视觉美感(25) + 观点表达(25)

等级判定: ≥85 优秀 / 60-84 合格 / <60 不合格
"""

import json
import os
import re
from datetime import datetime
from typing import Dict, List, Tuple, Optional


# ============================================================
# 文档类型识别
# ============================================================
def identify_doc_type(filename: str, content: str = '') -> str:
    """
    根据文件名和内容自动识别文档类型

    Returns:
        'creation_report' | 'literature_review' | 'proposal' |
        'translation' | 'admin_forms' | 'ppt' | 'unknown'
    """
    fname_lower = filename.lower()
    content_lower = content.lower() if content else ''

    # 文件名关键词匹配
    type_keywords = {
        'creation_report': ['创作报告', '论文', '毕业设计', '毕业论文', '设计报告',
                            'creation report', 'thesis', 'dissertation', 'final report'],
        'literature_review': ['文献综述', '文献回顾', 'literature review', 'review'],
        'proposal': ['开题报告', '开题', 'proposal', '选题报告'],
        'translation': ['外文翻译', '翻译', 'translation', '译文'],
        'admin_forms': ['申报表', '任务书', '进程表', '进度表', '中期检查', '指导记录',
                        'application', 'task book', 'progress'],
        'ppt': ['答辩', 'ppt', 'presentation', 'slides', '演示'],
    }

    scores = {}
    for doc_type, keywords in type_keywords.items():
        score = 0
        for kw in keywords:
            if kw in fname_lower:
                score += 3
            if kw in content_lower[:2000]:
                score += 1
        scores[doc_type] = score

    best_type = max(scores, key=scores.get)
    if scores[best_type] >= 2:
        return best_type
    return 'creation_report'  # 默认按创作报告处理


# ============================================================
# 内容完整性评分（满分40）
# ============================================================
class CompletenessScorer:
    """内容结构完整性检测"""

    # 各文档类型必需的章节结构
    REQUIRED_SECTIONS = {
        'creation_report': [
            ('摘要', r'(摘\s*要|摘要)'),
            ('关键词', r'(关键词|关键词)'),
            ('Abstract', r'(?i)abstract'),
            ('Keywords', r'(?i)keywords'),
            ('目录', r'(目\s*录|目录|Table of Contents)'),
            ('绪论/引言', r'(绪\s*论|引\s*言|前言|背景)'),
            ('正文主体', r'(第[一二三四五六七八九十\d]+章|系统设计|系统实现|功能设计|模块设计)'),
            ('结论/总结', r'(结\s*论|总\s*结|展望)'),
            ('致谢', r'(致\s*谢|谢\s*辞)'),
            ('参考文献', r'(参考文献|References)'),
        ],
        'literature_review': [
            ('摘要', r'(摘\s*要|摘要)'),
            ('关键词', r'(关键词)'),
            ('综述正文', r'(综\s*述|研究现状|研究进展|国内外)'),
            ('总结/述评', r'(总\s*结|述\s*评|展\s*望|小\s*结)'),
            ('参考文献', r'(参考文献|References)'),
        ],
        'proposal': [
            ('选题背景/意义', r'(选题背景|研究背景|选题意义|研究意义)'),
            ('国内外研究现状', r'(研究现状|文献综述|国内外)'),
            ('研究内容/目标', r'(研究内容|研究目标|主要内容)'),
            ('研究方法/技术路线', r'(研究方法|技术路线|实现方案)'),
            ('进度安排', r'(进度|时间安排|工作计划)'),
            ('参考文献', r'(参考文献|References)'),
        ],
        'translation': [
            ('原文', r'(原文|original|原文标题)'),
            ('译文', r'(译文|translation|translated)'),
            ('术语对照', r'(术语|glossary|terminology)'),
        ],
        'admin_forms': [
            ('课题名称', r'(课题名称|题目|项目名称)'),
            ('学生信息', r'(学号|姓名|班级|专业)'),
            ('指导教师', r'(指导教师|导师)'),
            ('日期/审批', r'(日期|审批|签[字名])'),
        ],
    }

    def __init__(self, config: dict = None):
        self.config = config or {}

    def score(self, filename: str, content: str) -> Tuple[int, List[dict]]:
        """
        对单份文档的内容完整性评分

        Returns:
            (得分, 缺失项列表)
        """
        doc_type = identify_doc_type(filename, content)
        required = self.REQUIRED_SECTIONS.get(doc_type, self.REQUIRED_SECTIONS['creation_report'])

        max_score = 40
        per_item = max_score / len(required) if required else max_score

        found_count = 0
        details = []

        for section_name, pattern in required:
            matches = re.findall(pattern, content, re.IGNORECASE)
            if matches:
                found_count += 1
                details.append({
                    'section': section_name,
                    'found': True,
                    'hint': f'找到匹配: {matches[0][:50]}',
                })
            else:
                details.append({
                    'section': section_name,
                    'found': False,
                    'hint': f'未找到"{section_name}"',
                })

        score = int(per_item * found_count)
        return score, details


# ============================================================
# 格式规范性评分（满分20）
# ============================================================
class FormatScorer:
    """文档格式规范性检测"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def score(self, content: str, manifest_entry: dict = None) -> Tuple[int, List[dict]]:
        """
        格式规范性评分

        检查项：
        1. 参考文献数量合理性（≥10篇）
        2. 图表编号检测（图X-Y / 表X-Y）
        3. 页数合理性
        4. 域代码残留检测
        5. 模板占位符检测
        """
        max_score = 20
        deductions = []

        # 1. 参考文献数量检测
        ref_count = len(re.findall(r'\[\d+\]', content))
        if ref_count < 10:
            deduct = 5
            deductions.append({
                'check': '参考文献数量',
                'issue': f'仅检测到 {ref_count} 条参考文献引用，建议≥10条',
                'deduction': deduct,
            })
        elif ref_count < 15:
            deduct = 2
            deductions.append({
                'check': '参考文献数量',
                'issue': f'参考文献 {ref_count} 条，建议补充至15条以上',
                'deduction': deduct,
            })

        # 2. 图表编号检测
        fig_count = len(re.findall(r'图\s*\d+[-\u3000]\d+', content))
        table_count = len(re.findall(r'表\s*\d+[-\u3000]\d+', content))
        if fig_count + table_count == 0 and len(content) > 5000:
            deduct = 3
            deductions.append({
                'check': '图表编号',
                'issue': '长文档中未检测到图表编号（图X-Y/表X-Y），建议补充',
                'deduction': deduct,
            })

        # 3. 模板占位符检测
        placeholder_signals = [
            '请更换', '此处写', '这里的格式', '请直接输入',
            '删除此句', '作品名字', '全小写', '请在此处',
            '替换为', 'XXX', '占位',
        ]
        found_placeholders = []
        for signal in placeholder_signals:
            if signal in content:
                found_placeholders.append(signal)

        if found_placeholders:
            deduct = min(5, len(found_placeholders) * 2)
            deductions.append({
                'check': '模板残留',
                'issue': f'检测到模板占位符: {", ".join(found_placeholders)}',
                'deduction': deduct,
            })

        # 4. 域代码残留检测（来自 batch_extract 的元数据）
        if manifest_entry and manifest_entry.get('domain_codes', 0) > 0:
            deduct = min(3, manifest_entry['domain_codes'])
            deductions.append({
                'check': '域代码残留',
                'issue': f'检测到 {manifest_entry["domain_codes"]} 处域代码残留(TOC/HYPERLINK/PAGEREF)',
                'deduction': deduct,
            })

        # 5. 英文标题与中文标题格式一致性
        chinese_title = re.findall(r'^[#\s]*[\u4e00-\u9fff].{5,50}$', content[:2000], re.MULTILINE)
        english_title = re.findall(r'^[#\s]*[A-Z][A-Za-z\s:-]{10,80}$', content[:2000], re.MULTILINE)
        if chinese_title and not english_title and len(content) > 3000:
            deduct = 1
            deductions.append({
                'check': '英文标题',
                'issue': '未检测到英文标题，建议补充',
                'deduction': deduct,
            })

        total_deduct = sum(d['deduction'] for d in deductions)
        score = max(0, max_score - total_deduct)
        return score, deductions


# ============================================================
# 逻辑一致性评分（满分20）
# ============================================================
class LogicScorer:
    """逻辑一致性检测"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def score(self, content: str) -> Tuple[int, List[dict]]:
        """
        基于以下指标评估逻辑一致性：
        1. 章节递进关系（一级标题数量）
        2. 逻辑连接词密度
        3. 论证标记词使用
        4. 总结/小结的分布
        """
        max_score = 20
        details = []
        total_score = 0

        # 1. 章节结构合理性（最多8分）
        # 检测章节层次
        chapters = re.findall(r'^[#\s]*第[一二三四五六七八九十\d]+章', content, re.MULTILINE)
        sections = re.findall(r'^\d+[\.\s]+[\u4e00-\u9fff]', content, re.MULTILINE)
        all_headings = len(chapters) + len(sections)

        if all_headings >= 4:
            chap_score = 8
            details.append({'check': '章节数量', 'result': f'{all_headings}个章节，结构完整', 'score': chap_score})
        elif all_headings >= 2:
            chap_score = 5
            details.append({'check': '章节数量', 'result': f'{all_headings}个章节，结构较简单', 'score': chap_score})
        else:
            chap_score = 2
            details.append({'check': '章节数量', 'result': f'仅{all_headings}个章节，层级不足', 'score': chap_score})
        total_score += chap_score

        # 2. 逻辑连接词密度（最多6分）
        logic_words = [
            '因此', '所以', '然而', '但是', '此外', '另外', '同时', '一方面', '另一方面',
            '首先', '其次', '最后', '综上所述', '总而言之', '由此可见', '换言之',
            '对比', '类似', '不同于', '相反', '进而', '从而', '基于', '由于',
            'therefore', 'however', 'moreover', 'furthermore', 'consequently',
            'in contrast', 'on the other hand', 'as a result', 'in conclusion',
        ]
        logic_count = sum(len(re.findall(re.escape(w), content, re.IGNORECASE)) for w in logic_words)
        content_len = len(content)

        if content_len > 0:
            density = logic_count / (content_len / 1000)  # 每千字逻辑词密度
            if density >= 5:
                logic_score = 6
            elif density >= 2.5:
                logic_score = 4
            elif density >= 1:
                logic_score = 2
            else:
                logic_score = 1
        else:
            density = 0
            logic_score = 0

        details.append({
            'check': '逻辑连接词密度',
            'result': f'每千字{density:.1f}个逻辑词',
            'score': logic_score,
        })
        total_score += logic_score

        # 3. 论证结构完整性（最多6分）
        # 检测是否有"提出问题→分析问题→解决问题"的结构
        intro_patterns = re.findall(r'(背景|问题|现状|需求|目标)', content[:len(content)//3])
        analysis_patterns = re.findall(r'(分析|设计|研究|方法|实验|实现|开发)', content[len(content)//3:2*len(content)//3])
        conclusion_patterns = re.findall(r'(结论|总结|展望|成果|效果|验证|测试)', content[2*len(content)//3:])

        arg_score = 0
        if intro_patterns:
            arg_score += 2
            details.append({'check': '论证-提出', 'result': '检测到提出问题/背景', 'score': 2})
        else:
            details.append({'check': '论证-提出', 'result': '未明确检测到背景/问题提出', 'score': 0})

        if analysis_patterns:
            arg_score += 2
            details.append({'check': '论证-分析', 'result': '检测到分析/设计/实现过程', 'score': 2})
        else:
            details.append({'check': '论证-分析', 'result': '未检测到分析/设计过程', 'score': 0})

        if conclusion_patterns:
            arg_score += 2
            details.append({'check': '论证-总结', 'result': '检测到结论/成果/验证', 'score': 2})
        else:
            details.append({'check': '论证-总结', 'result': '未检测到结论/验证', 'score': 0})

        total_score += arg_score

        return total_score, details


# ============================================================
# 论证合理性评分（满分20）
# ============================================================
class ArgumentScorer:
    """论证合理性检测"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def score(self, content: str) -> Tuple[int, List[dict]]:
        """
        评估论证质量：
        1. 文献引用密度
        2. 数据/图表支撑
        3. 批判性分析标记
        4. 创新点/贡献表述
        """
        max_score = 20
        total_score = 0
        details = []

        # 1. 文献引用密度（最多6分）
        ref_markers = re.findall(r'\[\d+\]', content)
        ref_density = len(ref_markers) / max(len(content) / 1000, 1)  # 每千字引用数

        if ref_density >= 3:
            ref_score = 6
        elif ref_density >= 1.5:
            ref_score = 4
        elif ref_density >= 0.5:
            ref_score = 2
        else:
            ref_score = 0
        details.append({
            'check': '文献引用密度',
            'result': f'每千字{ref_density:.1f}条引用',
            'score': ref_score,
        })
        total_score += ref_score

        # 2. 数据/图表支撑（最多5分）
        has_figures = bool(re.search(r'(如图|见图|如下表|图\s*\d|表\s*\d)', content))
        has_numbers = len(re.findall(r'\d+\.?\d*%', content)) >= 3  # 至少3个百分比数据
        has_code = bool(re.search(r'(代码|code|算法|algorithm|伪代码|函数|function)', content))

        data_score = 0
        if has_figures:
            data_score += 2
            details.append({'check': '图表支撑', 'result': '有图表引用', 'score': 2})
        if has_numbers:
            data_score += 2
            details.append({'check': '数据支撑', 'result': '有量化数据', 'score': 2})
        if has_code:
            data_score += 1
            details.append({'check': '代码/算法', 'result': '含代码或算法描述', 'score': 1})

        total_score += data_score

        # 3. 批判性分析（最多5分）
        critical_phrases = [
            '不足', '局限', '缺点', '改进', '对比', '优于', '不如',
            '不同之处', '差异', '优势', '劣势', '挑战', '问题',
            'limitation', 'drawback', 'advantage', 'disadvantage', 'compare',
        ]
        critical_count = sum(len(re.findall(p, content, re.IGNORECASE)) for p in critical_phrases)

        if critical_count >= 5:
            critical_score = 5
        elif critical_count >= 2:
            critical_score = 3
        else:
            critical_score = 1

        details.append({
            'check': '批判性分析',
            'result': f'{critical_count}处批判性/对比分析表述',
            'score': critical_score,
        })
        total_score += critical_score

        # 4. 创新点/贡献表述（最多4分）
        innovation_phrases = [
            '创新', '改进', '提出', '设计', '实现', '优化', '首次', '新颖',
            '贡献', '创新点', '亮点', '特色',
            'novel', 'contribution', 'improvement', 'proposed',
        ]
        inno_count = sum(len(re.findall(p, content, re.IGNORECASE)) for p in innovation_phrases)

        if inno_count >= 3:
            inno_score = 4
        elif inno_count >= 1:
            inno_score = 2
        else:
            inno_score = 0

        details.append({
            'check': '创新/贡献表述',
            'result': f'{inno_count}处创新/贡献相关表述',
            'score': inno_score,
        })
        total_score += inno_score

        return min(max_score, total_score), details


# ============================================================
# 外文翻译评分
# ============================================================
class TranslationScorer:
    """外文翻译质量评估"""

    def __init__(self, config: dict = None):
        self.config = config or {}

    def score(self, content: str, manifest_entry: dict = None) -> Tuple[int, int, List[dict]]:
        """
        对外文翻译文档评分

        Returns:
            (规范性得分, 翻译质量得分, 详情列表)
        """
        details = []

        # 格式规范性（满分30）
        format_score = 30
        format_deductions = []

        # 检测原文是否存在
        if not re.search(r'原文|original', content, re.IGNORECASE):
            format_deductions.append({
                'check': '原文存在性',
                'issue': '未找到原文部分',
                'deduction': 10,
            })

        # 检测译文是否存在
        if not re.search(r'译文|翻译|translated', content, re.IGNORECASE):
            format_deductions.append({
                'check': '译文存在性',
                'issue': '未找到译文部分',
                'deduction': 10,
            })

        format_score -= sum(d['deduction'] for d in format_deductions)
        format_score = max(0, format_score)

        # 翻译质量（满分30，基于内容长度和结构完整性估算）
        quality_score = 15  # 基准分

        # 内容长度合理性
        content_len = len(content)
        if content_len > 5000:
            quality_score += 10
            details.append({'check': '内容完整度', 'result': '内容充足', 'score': 10})
        elif content_len > 2000:
            quality_score += 5
            details.append({'check': '内容完整度', 'result': '内容适中', 'score': 5})
        else:
            details.append({'check': '内容完整度', 'result': '内容较少', 'score': 0})

        # 术语对照表检测
        if re.search(r'术语|glossary|terminology|对照', content, re.IGNORECASE):
            quality_score += 5
            details.append({'check': '术语对照表', 'result': '含术语对照', 'score': 5})

        quality_score = min(30, quality_score)
        details.extend(format_deductions)

        return format_score, quality_score, details


# ============================================================
# 评分聚合引擎
# ============================================================
class ScoringEngine:
    """
    评分计算引擎 — 全代码模式核心

    对毕业设计存档目录中的所有文档进行多维度自动评分，
    聚合单文档得分，计算综合总分和等级。
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.completeness_scorer = CompletenessScorer(config)
        self.format_scorer = FormatScorer(config)
        self.logic_scorer = LogicScorer(config)
        self.argument_scorer = ArgumentScorer(config)
        self.translation_scorer = TranslationScorer(config)

        # 评分权重
        dims = self.config.get('scoring', {})
        self.creation_dims = dims.get('creation_report', {'完整性': 40, '规范性': 20, '逻辑性': 20, '论证': 20})
        self.review_dims = dims.get('literature_review', {'完整性': 40, '规范性': 20, '逻辑性': 20, '论证': 20})
        self.proposal_dims = dims.get('proposal', {'完整性': 40, '规范性': 20, '逻辑性': 20, '论证': 20})

        # 综合权重
        self.final_weights = self.config.get('weights', {
            'creation_report': 0.35,
            'literature_review': 0.20,
            'proposal': 0.15,
            'translation': 0.10,
            'admin_forms': 0.05,
        })

    def score_all(self, manifest: list) -> dict:
        """
        对所有文档执行完整评分流水线

        Args:
            manifest: batch_extract.py 生成的 manifest 列表

        Returns:
            {
                'single_doc_scores': {文档类型: {维度得分}},
                'final_score': 综合总分,
                'grade': 等级,
                'issues': 问题列表,
                'scoring_details': 评分详情,
            }
        """
        single_scores = {}
        all_issues = []

        for item in manifest:
            filename = item['file']
            txt_path = item.get('txt_path', '')

            if not txt_path or not os.path.exists(txt_path):
                continue

            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            doc_type = identify_doc_type(filename, content)

            if doc_type in ('creation_report', 'literature_review', 'proposal'):
                scores, issues = self._score_full_doc(filename, content, doc_type, item)
            elif doc_type == 'translation':
                scores, issues = self._score_translation(filename, content, item)
            elif doc_type == 'admin_forms':
                scores, issues = self._score_admin_form(filename, content, item)
            else:
                continue

            single_scores[filename] = scores
            all_issues.extend(issues)

        # 去掉文件名，用文档类型聚合
        doc_type_scores = self._aggregate_by_type(single_scores)

        # 计算综合总分
        final_score = self._calculate_final(doc_type_scores)
        grade = self._determine_grade(final_score)

        return {
            'single_doc_scores': doc_type_scores,
            'final_score': final_score,
            'grade': grade,
            'issues': all_issues,
        }

    def _score_full_doc(self, filename: str, content: str,
                         doc_type: str, manifest_entry: dict) -> Tuple[dict, list]:
        """对完整文档（创作报告/文献综述/开题报告）进行四维度评分"""
        issues = []

        # 根据文档类型选择评分维度
        if doc_type == 'creation_report':
            dims = self.creation_dims
        elif doc_type == 'literature_review':
            dims = self.review_dims
        else:
            dims = self.proposal_dims

        # 内容完整性
        completeness_score, comp_details = self.completeness_scorer.score(filename, content)
        for d in comp_details:
            if not d['found']:
                issues.append({
                    'file': filename,
                    'issue': f'缺失章节: {d["section"]}',
                    'evidence': d['hint'],
                    'confidence': '高',
                    'dimension': '完整性',
                })

        # 格式规范性
        format_score, format_details = self.format_scorer.score(content, manifest_entry)
        for d in format_details:
            if d.get('deduction', 0) > 0:
                issues.append({
                    'file': filename,
                    'issue': d['issue'],
                    'evidence': d['check'],
                    'confidence': '高',
                    'dimension': '规范性',
                })

        # 逻辑一致性
        logic_score, logic_details = self.logic_scorer.score(content)

        # 论证合理性
        argument_score, arg_details = self.argument_scorer.score(content)

        # 缩放至配置的满分值
        scaled = {
            '完整性': self._scale_score(completeness_score, 40, dims.get('完整性', 40)),
            '规范性': self._scale_score(format_score, 20, dims.get('规范性', 20)),
            '逻辑性': self._scale_score(logic_score, 20, dims.get('逻辑性', 20)),
            '论证': self._scale_score(argument_score, 20, dims.get('论证', 20)),
        }
        scaled['总分'] = sum(scaled.values())

        return scaled, issues

    def _score_translation(self, filename: str, content: str,
                            manifest_entry: dict) -> Tuple[dict, list]:
        """对外文翻译评分"""
        format_score, quality_score, details = self.translation_scorer.score(content, manifest_entry)

        issues = []
        for d in details:
            if d.get('deduction', 0) > 0:
                issues.append({
                    'file': filename,
                    'issue': d.get('issue', d.get('result', '')),
                    'confidence': '中',
                    'dimension': '规范/质量',
                })

        scores = {
            '规范性': format_score,
            '翻译质量': quality_score,
            '总分': format_score + quality_score,
        }
        return scores, issues

    def _score_admin_form(self, filename: str, content: str,
                           manifest_entry: dict) -> Tuple[dict, list]:
        """对申报表/任务书等管理表单评分"""
        completeness_score, comp_details = self.completeness_scorer.score(filename, content)

        issues = []
        for d in comp_details:
            if not d['found']:
                issues.append({
                    'file': filename,
                    'issue': f'栏位缺失: {d["section"]}',
                    'evidence': d['hint'],
                    'confidence': '高',
                    'dimension': '栏位完整性',
                })

        # 管理表单满分40
        scaled_completeness = self._scale_score(completeness_score, 40, 20)

        format_score = 20
        if len(content) < 200:
            format_score = 10
            issues.append({
                'file': filename,
                'issue': '表单内容过少，可能未完整填写',
                'evidence': f'仅{len(content)}字符',
                'confidence': '中',
                'dimension': '规范性',
            })

        scores = {
            '规范性': format_score,
            '栏位完整性': scaled_completeness,
            '总分': format_score + scaled_completeness,
        }
        return scores, issues

    def _scale_score(self, raw: int, raw_max: int, target_max: int) -> int:
        """分数缩放"""
        if raw_max == 0:
            return 0
        return int(raw * target_max / raw_max)

    def _aggregate_by_type(self, single_scores: dict) -> dict:
        """将按文件名的评分聚合为按文档类型"""
        aggregated = {}
        for filename, scores in single_scores.items():
            doc_type = identify_doc_type(filename)
            type_names = {
                'creation_report': '创作报告',
                'literature_review': '文献综述',
                'proposal': '开题报告',
                'translation': '外文翻译',
                'admin_forms': '管理表单',
            }
            type_name = type_names.get(doc_type, doc_type)

            if type_name not in aggregated:
                aggregated[type_name] = scores
            else:
                # 同类型多文档取平均
                existing = aggregated[type_name]
                for key in scores:
                    if key in existing and isinstance(existing[key], (int, float)):
                        existing[key] = int((existing[key] + scores[key]) / 2)

        return aggregated

    def _calculate_final(self, doc_type_scores: dict) -> int:
        """计算综合总分"""
        if not doc_type_scores:
            return 0

        total = 0
        total_weight = 0

        type_to_key = {
            '创作报告': 'creation_report',
            '文献综述': 'literature_review',
            '开题报告': 'proposal',
            '外文翻译': 'translation',
            '管理表单': 'admin_forms',
        }

        for type_name, scores in doc_type_scores.items():
            key = type_to_key.get(type_name)
            if key and key in self.final_weights:
                weight = self.final_weights[key]
                doc_total = scores.get('总分', 0)
                total += doc_total * weight
                total_weight += weight

        if total_weight > 0:
            return int(total / total_weight)
        return 0

    def _determine_grade(self, score: int) -> str:
        """等级判定"""
        if score >= 85:
            return '优秀'
        elif score >= 60:
            return '合格'
        else:
            return '不合格'


# ============================================================
# 独立运行支持
# ============================================================
def main():
    """独立运行：对指定文件或目录评分"""
    import argparse

    parser = argparse.ArgumentParser(description='评分计算引擎')
    parser.add_argument('--txt-dir', default='/tmp/auto_grading', help='提取文本目录')
    parser.add_argument('--output', help='输出JSON路径')
    args = parser.parse_args()

    # 加载 manifest
    manifest_path = os.path.join(args.txt_dir, '_manifest.json')
    if not os.path.exists(manifest_path):
        print(f'[ERROR] manifest 不存在: {manifest_path}')
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    # 评分
    engine = ScoringEngine()
    result = engine.score_all(manifest)

    # 输出
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f'\n评分结果已保存至: {args.output}')

    # 汇总
    print(f"\n综合总分: {result['final_score']}  |  等级: {result['grade']}")
    print(f"问题数: {len(result['issues'])}")


if __name__ == '__main__':
    main()
