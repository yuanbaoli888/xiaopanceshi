#!/usr/bin/env python3
"""
毕业设计多文档智能自动审查系统 — 主程序入口 v1.0

支持双模运行：
  --mode=skill  低代码模式（调用 LLM 评审引擎，日常使用）
  --mode=code   全代码模式（规则引擎离线评审，软著登记用）

用法：
  python main.py review --archive-dir ./张三/ --mode=code
  python main.py review --archive-dir ./张三/ --mode=skill
  python main.py review --archive-dir ./张三/ --mode=both --diff
  python main.py batch --base-dir ./学生存档/ --mode=code
  python main.py config --show
  python main.py config --set scoring.creation_report.完整性:45
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


# ============================================================
# 项目路径解析
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT / 'scripts'
TEMPLATES_DIR = PROJECT_ROOT / 'templates'
REFERENCES_DIR = PROJECT_ROOT / 'references'
sys.path.insert(0, str(SCRIPTS_DIR))


# ============================================================
# 配置加载
# ============================================================
def load_config():
    """加载 YAML/JSON 配置文件"""
    config_path = PROJECT_ROOT / 'config.yaml'
    if not config_path.exists():
        config_path = PROJECT_ROOT / 'config.json'

    default_config = {
        'scoring': {
            'creation_report': {'完整性': 40, '规范性': 20, '逻辑性': 20, '论证': 20},
            'literature_review': {'完整性': 40, '规范性': 20, '逻辑性': 20, '论证': 20},
            'proposal': {'完整性': 40, '规范性': 20, '逻辑性': 20, '论证': 20},
            'translation': {'规范性': 30, '翻译质量': 30},
            'admin_forms': {'规范性': 20, '栏位完整性': 20},
        },
        'weights': {
            'creation_report': 0.35,
            'literature_review': 0.20,
            'proposal': 0.15,
            'translation': 0.10,
            'admin_forms': 0.05,
            'cross_check': 0.10,
            'ppt': 0.05,
        },
        'harness': {
            'l1_enabled': True,
            'l2_enabled': True,
            'l3_enabled': True,
            'l2_fuzzy_threshold': 0.6,
        },
        'gbt7714': {
            'rules': {f'r{i:02d}': True for i in range(1, 16)},
        },
        'llm': {
            'api_base': os.environ.get('LLM_API_BASE', 'https://api.openai.com/v1'),
            'api_key': os.environ.get('LLM_API_KEY', ''),
            'model': os.environ.get('LLM_MODEL', 'gpt-4o'),
            'temperature': 0.1,
            'max_tokens': 4096,
        },
        'output': {
            'default_formats': ['md', 'html'],
            'report_dir': str(PROJECT_ROOT / 'reports'),
        },
        'paths': {
            'temp_dir': str(PROJECT_ROOT / '.runtime' / 'auto_grading'),
            'log_dir': str(PROJECT_ROOT / 'logs'),
        },
    }

    if not config_path.exists():
        return default_config

    if config_path.suffix == '.yaml':
        try:
            import yaml
            with open(config_path, 'r', encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}
            return _deep_merge(default_config, yaml_config)
        except ImportError:
            pass
    elif config_path.suffix == '.json':
        with open(config_path, 'r', encoding='utf-8') as f:
            json_config = json.load(f)
        return _deep_merge(default_config, json_config)

    return default_config


def _deep_merge(base, override):
    """深度合并字典"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


CONFIG = load_config()


# ============================================================
# 步骤① 文档批量提取（共用模块）
# ============================================================
def step_extract(archive_dir: str, temp_dir: str = None) -> dict:
    """调用 batch_extract.py 提取文档文本"""
    from batch_extract import extract_directory

    temp_dir = temp_dir or CONFIG['paths']['temp_dir']
    extract_directory(archive_dir, temp_dir)

    manifest_path = os.path.join(temp_dir, '_manifest.json')
    if os.path.exists(manifest_path):
        with open(manifest_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


# ============================================================
# 步骤② 单文档评分
# ============================================================
def step_scoring(manifest: list, mode: str = 'code') -> dict:
    """
    对每份文档进行独立评分
    mode='code': 使用规则引擎 scoring_engine
    mode='skill': 使用 LLM 评审
    """
    if mode == 'code':
        try:
            from scoring_engine import ScoringEngine
            engine = ScoringEngine(CONFIG)
            return engine.score_all(manifest)
        except ImportError as e:
            print(f'[WARN] scoring_engine 未找到，降级为 skill 模式: {e}')
            return step_scoring(manifest, mode='skill')

    elif mode == 'skill':
        return _llm_scoring(manifest)

    else:
        raise ValueError(f"未知模式: {mode}")


def _llm_scoring(manifest: list) -> dict:
    """低代码模式：调用 LLM 进行单文档评分"""
    import subprocess
    prompt = _build_scoring_prompt(manifest)
    result = _call_llm(prompt)
    return result


def _build_scoring_prompt(manifest: list) -> str:
    """构建评分 prompt，注入评审模板"""
    template_path = REFERENCES_DIR / 'review_template.md'
    template_text = ''
    if template_path.exists():
        with open(template_path, 'r', encoding='utf-8') as f:
            template_text = f.read()

    docs_text = ''
    for item in manifest:
        txt_path = item.get('txt_path', '')
        if txt_path and os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()[:8000]
            docs_text += f"\n### {item['file']}\n{content}\n"

    return f"""按照以下评审模板对毕业设计文档进行评分。

## 评审模板
{template_text}

## 待评审文档
{docs_text}

请输出JSON格式的评分结果。"""


def _call_llm(prompt: str) -> dict:
    """调用 LLM API"""
    import urllib.request
    import urllib.error

    api_base = CONFIG['llm']['api_base']
    api_key = CONFIG['llm']['api_key']
    model = CONFIG['llm']['model']

    if not api_key:
        print('[WARN] LLM API Key 未配置，返回空评分')
        return {'single_doc_scores': {}, 'issues': []}

    payload = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': CONFIG['llm']['temperature'],
        'max_tokens': CONFIG['llm']['max_tokens'],
    }).encode('utf-8')

    req = urllib.request.Request(
        f'{api_base}/chat/completions',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        content = result['choices'][0]['message']['content']
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {'single_doc_scores': {}, 'issues': [], '_raw': content}
    except Exception as e:
        print(f'[ERROR] LLM 调用失败: {e}')
        return {'single_doc_scores': {}, 'issues': []}


# ============================================================
# 步骤③ 跨文档交叉审查
# ============================================================
def step_cross_check(manifest: list, mode: str = 'code') -> dict:
    """跨文档一致性检查"""
    if mode == 'code':
        try:
            from cross_checker import CrossChecker
            checker = CrossChecker(CONFIG)
            return checker.check_all(manifest)
        except ImportError as e:
            print(f'[WARN] cross_checker 未找到，降级为 skill 模式: {e}')
            return step_cross_check(manifest, mode='skill')
    elif mode == 'skill':
        return _llm_cross_check(manifest)
    return {}


def _llm_cross_check(manifest: list) -> dict:
    """LLM 跨文档交叉审查"""
    docs = ''
    for item in manifest:
        txt_path = item.get('txt_path', '')
        if txt_path and os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
                docs += f"\n--- {item['file']} ---\n{f.read()[:5000]}\n"

    prompt = f"""对以下毕业设计文档进行跨文档交叉审查，检查：
1. 要素一致性：学生姓名/学号/课题名称/指导教师在所有文档中是否一致
2. 日期逻辑：开题<中期<答辩<提交的时间顺序是否正确
3. 引用一致性：各文档参考文献重叠度是否合理

文档内容：
{docs}

请输出JSON格式。"""

    return _call_llm(prompt)


# ============================================================
# 步骤④ GB/T 7714 参考文献校验
# ============================================================
def step_gbt7714(manifest: list, mode: str = 'code') -> list:
    """GB/T 7714 参考文献格式校验"""
    if mode == 'code':
        try:
            from gbt7714_validator import GBT7714Validator
            validator = GBT7714Validator(CONFIG)
            return validator.validate_all(manifest)
        except ImportError:
            return []
    return []


# ============================================================
# 步骤⑤ 格式规范检查
# ============================================================
def step_format_check(manifest: list, mode: str = 'code') -> list:
    """格式规范检查"""
    if mode == 'code':
        try:
            from format_checker import FormatChecker
            checker = FormatChecker(CONFIG)
            return checker.check_all(manifest)
        except ImportError:
            return []
    return []


# ============================================================
# 步骤⑥ PPT 分析
# ============================================================
def step_ppt_analysis(manifest: list, mode: str = 'code') -> dict:
    """PPT 答辩分析"""
    pptx_items = [item for item in manifest if item.get('ext') == '.pptx']
    if not pptx_items:
        return {'ppt_review': {}}

    if mode == 'code':
        try:
            from ppt_analyzer import PPTAnalyzer
            analyzer = PPTAnalyzer(CONFIG)
            return analyzer.analyze(pptx_items[0])
        except ImportError:
            return {'ppt_review': {}}
    return {'ppt_review': {}}


# ============================================================
# 步骤⑦ 安全护栏
# ============================================================
def step_harness(issues: list, archive_dir: str = None, temp_dir: str = None) -> dict:
    """运行安全护栏校验"""
    try:
        from harness import run_harness

        temp_dir = temp_dir or CONFIG['paths']['temp_dir']
        os.makedirs(temp_dir, exist_ok=True)
        issues_path = os.path.join(temp_dir, '_raw_issues.json')
        with open(issues_path, 'w', encoding='utf-8') as f:
            json.dump({'issues': issues}, f, ensure_ascii=False, indent=2)

        result = run_harness(
            issues_path,
            txt_dir=temp_dir,
            archive_dir=archive_dir,
        )
        return result
    except ImportError:
        return {'clean_issues': issues, 'total': len(issues), 'passed': len(issues)}


# ============================================================
# 步骤⑧ 报告生成
# ============================================================
def step_report(result: dict, output_dir: str, formats: list = None):
    """生成评审报告"""
    if formats is None:
        formats = CONFIG['output']['default_formats']

    os.makedirs(output_dir, exist_ok=True)

    try:
        from report_generator import ReportGenerator
        generator = ReportGenerator(CONFIG)
        report_paths = generator.render(result, output_dir, formats)
        return report_paths
    except ImportError:
        # 降级：直接输出 JSON + 简单 Markdown
        paths = []
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        json_path = os.path.join(output_dir, f'review_{timestamp}.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        paths.append(json_path)

        if 'md' in formats:
            md_path = os.path.join(output_dir, f'review_{timestamp}.md')
            _render_simple_markdown(result, md_path)
            paths.append(md_path)

        return paths


def _render_simple_markdown(result: dict, path: str):
    """简易 Markdown 报告（降级方案）"""
    lines = ['# 毕业设计多文档智能自动审查报告\n']
    student = result.get('student_info', {}).get('name', '未知')
    lines.append(f'**学生**: {student}  |  **审查日期**: {datetime.now().strftime("%Y-%m-%d")}\n')

    scores = result.get('single_doc_scores', {})
    if scores:
        lines.append('## 单文档评分\n')
        lines.append('| 文档 | 完整性 | 规范性 | 逻辑性 | 论证 | 总分 |')
        lines.append('|------|:---:|:---:|:---:|:---:|:---:|')
        for doc, s in scores.items():
            if isinstance(s, dict):
                c = s.get('完整性', '-')
                f = s.get('规范性', '-')
                l = s.get('逻辑性', '-')
                a = s.get('论证', '-')
                t = s.get('总分', '-')
                lines.append(f'| {doc} | {c} | {f} | {l} | {a} | {t} |')
        lines.append('')

    issues = result.get('clean_issues', [])
    if issues:
        lines.append(f'## 需修改项（共 {len(issues)} 条）\n')
        for i, issue in enumerate(issues, 1):
            lines.append(f"{i}. **{issue.get('file', '?')}** — {issue.get('issue', '?')}")
            lines.append(f"   > {issue.get('evidence', '?')}  `置信度: {issue.get('confidence', '?')}`\n")

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ============================================================
# 审查流水线（完整编排）
# ============================================================
def run_review_pipeline(archive_dir: str, output_dir: str = None,
                         mode: str = 'code', formats: list = None):
    """
    执行完整审查流水线

    Args:
        archive_dir: 学生存档目录路径
        output_dir: 报告输出目录
        mode: 'code' | 'skill' | 'both'
        formats: ['md', 'html', 'pdf', 'xlsx', 'json']

    Returns:
        dict: 完整的审查结果
    """
    if output_dir is None:
        output_dir = CONFIG['output']['report_dir']

    start_time = time.time()
    print(f'\n{"="*60}')
    print(f'  毕业设计多文档智能自动审查系统 v1.0')
    print(f'  模式: {mode.upper()}')
    print(f'  存档目录: {archive_dir}')
    print(f'  输出目录: {output_dir}')
    print(f'{"="*60}\n')

    result = {
        'student_info': {'archive_dir': archive_dir},
        'review_date': datetime.now().isoformat(),
        'mode': mode,
        'single_doc_scores': {},
        'cross_check': {},
        'gbt7714_issues': [],
        'format_issues': [],
        'ppt_review': {},
        'issues': [],
        'clean_issues': [],
        'harness_report': {},
        'report_paths': [],
    }

    # ① 文档提取
    print('[1/8] 正在提取文档...')
    manifest = step_extract(archive_dir)
    if not manifest:
        print('[ERROR] 未找到可解析的文档文件')
        return result
    result['manifest'] = manifest
    print(f'  ✅ 提取完成，共 {len(manifest)} 个文件')
    for m in manifest:
        print(f'     {m["file"]} ({m.get("ext", "?")}, {len(open(m["txt_path"]).read()) if os.path.exists(m.get("txt_path","")) else 0} 字符)')

    # ② 单文档评分
    print('\n[2/8] 正在执行单文档评分...')
    scores = step_scoring(manifest, mode)
    result['single_doc_scores'] = scores.get('single_doc_scores', {})
    print(f'  ✅ 已评分 {len(result["single_doc_scores"])} 份文档')

    # ③ 跨文档交叉审查
    print('\n[3/8] 正在执行跨文档交叉审查...')
    cross = step_cross_check(manifest, mode)
    result['cross_check'] = cross
    print(f'  ✅ 交叉审查完成')

    # ④ GB/T 7714 校验
    print('\n[4/8] 正在校验参考文献格式 (GB/T 7714-2015)...')
    ref_issues = step_gbt7714(manifest, mode)
    result['gbt7714_issues'] = ref_issues
    print(f'  ✅ 发现 {len(ref_issues)} 条参考文献格式问题')

    # ⑤ 格式规范检查
    print('\n[5/8] 正在检查文档格式规范...')
    fmt_issues = step_format_check(manifest, mode)
    result['format_issues'] = fmt_issues
    print(f'  ✅ 发现 {len(fmt_issues)} 条格式问题')

    # ⑥ PPT 分析
    print('\n[6/8] 正在分析答辩PPT...')
    ppt = step_ppt_analysis(manifest, mode)
    result['ppt_review'] = ppt.get('ppt_review', {})
    print(f'  ✅ PPT分析完成')

    # 汇总 issues
    all_issues = scores.get('issues', []) + ref_issues + fmt_issues
    result['issues'] = all_issues

    # ⑦ 安全护栏
    print('\n[7/8] 正在运行安全护栏 (HARNESS)...')
    harness_result = step_harness(all_issues, archive_dir)
    result['harness_report'] = harness_result
    result['clean_issues'] = harness_result.get('clean_issues', all_issues)
    passed = harness_result.get('passed', 0)
    failed = harness_result.get('failed', 0)
    print(f'  ✅ 护栏完成: {passed}条通过 / {failed}条拒绝')

    # ⑧ 报告生成
    print('\n[8/8] 正在生成评审报告...')
    report_paths = step_report(result, output_dir, formats)
    result['report_paths'] = report_paths
    for p in report_paths:
        print(f'  📄 {p}')

    elapsed = time.time() - start_time
    result['elapsed_seconds'] = round(elapsed, 1)
    print(f'\n{"="*60}')
    print(f'  审查完成！耗时 {elapsed:.1f} 秒')
    print(f'{"="*60}\n')

    return result


def run_batch_pipeline(base_dir: str, output_dir: str = None,
                        mode: str = 'code', formats: list = None):
    """
    批量审查多个学生存档

    Args:
        base_dir: 包含多个学生子目录的根目录
    """
    if output_dir is None:
        output_dir = CONFIG['output']['report_dir']

    student_dirs = sorted([
        d for d in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, d)) and not d.startswith('.')
    ])

    if not student_dirs:
        print(f'[ERROR] 在 {base_dir} 中未找到学生存档子目录')
        return []

    print(f'\n批量审查模式: 共 {len(student_dirs)} 个学生')
    print(f'学生列表: {", ".join(student_dirs)}\n')

    results = []
    for i, student in enumerate(student_dirs, 1):
        student_dir = os.path.join(base_dir, student)
        student_output = os.path.join(output_dir, student)

        print(f'\n{"#"*60}')
        print(f'  [{i}/{len(student_dirs)}] 正在审查: {student}')
        print(f'{"#"*60}')

        try:
            result = run_review_pipeline(student_dir, student_output, mode, formats)
            results.append(result)
        except Exception as e:
            print(f'[ERROR] 审查 {student} 时发生异常: {e}')
            import traceback
            traceback.print_exc()

    # 批量汇总
    summary_path = os.path.join(output_dir, 'batch_summary.json')
    summary = {
        'total_students': len(student_dirs),
        'reviewed': len(results),
        'failed': len(student_dirs) - len(results),
        'date': datetime.now().isoformat(),
        'results': [],
    }
    for r in results:
        student = os.path.basename(r['student_info']['archive_dir'])
        summary['results'].append({
            'student': student,
            'single_doc_scores': r.get('single_doc_scores', {}),
            'issues_count': len(r.get('clean_issues', [])),
            'elapsed_seconds': r.get('elapsed_seconds', 0),
        })

    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n📊 批量审查汇总: {summary_path}')

    return results


# ============================================================
# 配置管理子命令
# ============================================================
def handle_config(args):
    """处理 config 子命令"""
    if args.show:
        dotted = args.show
        value = CONFIG
        for key in dotted.split('.'):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                print(f'配置项 {dotted} 不存在')
                return
        print(json.dumps(value, ensure_ascii=False, indent=2))

    elif args.set:
        if ':' not in args.set:
            print('格式错误。用法: --set key.subkey:value')
            return
        key_path, val_str = args.set.split(':', 1)
        keys = key_path.split('.')

        try:
            val = json.loads(val_str)
        except (json.JSONDecodeError, ValueError):
            val = val_str

        target = CONFIG
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        target[keys[-1]] = val

        config_path = PROJECT_ROOT / 'config.json'
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        print(f'✅ 已更新: {key_path} = {val}')

    elif args.list:
        print(json.dumps(CONFIG, ensure_ascii=False, indent=2))


# ============================================================
# CLI 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='毕业设计多文档智能自动审查系统 v1.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py review --archive-dir ./张三/
  python main.py review --archive-dir ./张三/ --mode=code --format pdf,html
  python main.py batch --base-dir ./学生存档/
  python main.py config --show scoring
        """,
    )

    subparsers = parser.add_subparsers(dest='command', help='子命令')

    # review 子命令
    review_parser = subparsers.add_parser('review', help='审查单个学生存档')
    review_parser.add_argument('--archive-dir', required=True, help='学生存档目录路径')
    review_parser.add_argument('--output', '-o', help='报告输出目录')
    review_parser.add_argument('--mode', choices=['code', 'skill', 'both'], default='code',
                                help='审查模式: code=全代码规则引擎, skill=LLM评审, both=双模对比')
    review_parser.add_argument('--format', '-f', default='md,json',
                                help='报告格式: md,html,pdf,xlsx,json (逗号分隔)')
    review_parser.add_argument('--verbose', '-v', action='store_true', help='详细日志')

    # batch 子命令
    batch_parser = subparsers.add_parser('batch', help='批量审查多个学生存档')
    batch_parser.add_argument('--base-dir', required=True, help='包含多个学生子目录的根目录')
    batch_parser.add_argument('--output', '-o', help='报告输出根目录')
    batch_parser.add_argument('--mode', choices=['code', 'skill', 'both'], default='code')
    batch_parser.add_argument('--format', '-f', default='md,json')

    # config 子命令
    config_parser = subparsers.add_parser('config', help='配置管理')
    config_parser.add_argument('--show', help='显示指定配置项（点号分隔）')
    config_parser.add_argument('--set', help='设置配置项（格式: key.subkey:value）')
    config_parser.add_argument('--list', '-l', action='store_true', help='列出全部配置')

    args = parser.parse_args()

    if args.command == 'review':
        formats = [f.strip() for f in args.format.split(',')]
        result = run_review_pipeline(
            archive_dir=args.archive_dir,
            output_dir=args.output,
            mode=args.mode,
            formats=formats,
        )
        if args.verbose:
            print('\n完整审查结果:')
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.command == 'batch':
        formats = [f.strip() for f in args.format.split(',')]
        run_batch_pipeline(
            base_dir=args.base_dir,
            output_dir=args.output,
            mode=args.mode,
            formats=formats,
        )

    elif args.command == 'config':
        handle_config(args)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
