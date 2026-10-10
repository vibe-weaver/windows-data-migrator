"""链接重建策略：目标分类、改写、分组与创建。"""
from __future__ import annotations

import os

from . import reparse
from .fsutil import is_inside, norm_target
from .models import LinkTargetClass, ResolvedTarget, TargetGroup


def apply_mappings(path: str, mappings: dict[str, str]) -> str:
    """按最长前缀优先应用用户映射，且保证路径分量边界正确。"""
    best_src = ""
    best_dst = ""
    for src, dst in mappings.items():
        src_n = os.path.normpath(src)
        if is_inside(path, src_n) and len(os.path.normpath(src_n)) > len(best_src):
            best_src, best_dst = os.path.normpath(src_n), os.path.normpath(dst)
    if not best_src:
        return path
    rest = path[len(best_src):].lstrip("\\/")
    return os.path.join(best_dst, rest) if rest else best_dst


def resolve_target(raw_target: str, link_abs: str, source_root: str,
                   target_root: str, mappings: dict[str, str]) -> ResolvedTarget:
    """按规则算出链接在目标端应写入的目标路径。

    三条规则：
      1. 相对路径 → 原样保留（结构不变，相对关系自然保持）
      2. 源树内的绝对路径 → 把源根前缀替换成目标根
      3. 树外绝对路径 → 默认原样，除非用户指定了前缀映射

    「与源树的关系」和「目标是否存在」分开返回：树外目标同样可能是断链的。
    """
    raw = norm_target(raw_target)
    source_root = os.path.normpath(str(source_root))
    target_root = os.path.normpath(str(target_root))

    if not os.path.isabs(raw):
        # 相对路径按链接所在目录解析，仅用于判断是否断链
        resolved = os.path.normpath(os.path.join(os.path.dirname(link_abs), raw))
        return ResolvedTarget(raw, LinkTargetClass.RELATIVE,
                              is_dangling=not os.path.exists(resolved),
                              origin_target=raw)

    resolved = os.path.normpath(raw)
    if is_inside(resolved, source_root):
        rel = os.path.relpath(resolved, source_root)
        new = os.path.normpath(os.path.join(target_root, rel))
        return ResolvedTarget(new, LinkTargetClass.INSIDE_SOURCE,
                              is_dangling=not os.path.exists(resolved),
                              origin_target=resolved)

    mapped = apply_mappings(resolved, mappings)
    return ResolvedTarget(mapped, LinkTargetClass.OUTSIDE_SOURCE,
                          is_dangling=not os.path.exists(mapped),
                          origin_target=resolved)


def _common_prefix(paths: list[str]) -> str:
    """求若干绝对路径的最长公共目录前缀（按分量边界）。"""
    if not paths:
        return ""
    splits = [os.path.normpath(p).split(os.sep) for p in paths]
    common: list[str] = []
    for parts in zip(*splits):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    if not common:
        return ""
    prefix = os.sep.join(common)
    if len(common) == 1 and common[0].endswith(":"):
        prefix += os.sep
    return prefix


def _best_mapping_key(path: str, mappings: dict[str, str]) -> str:
    """返回命中该路径的最深映射键；没有命中返回空串。"""
    best = ""
    for src in mappings:
        src_n = os.path.normpath(src)
        if is_inside(path, src_n) and len(src_n) > len(best):
            best = src_n
    return best


def group_targets(targets: list[str],
                  mappings: dict[str, str] | None = None) -> list[TargetGroup]:
    """把链接目标聚合成归属表的行。

    传入的 targets 必须是**映射前**的原始目标：分组键稳定了，用户填过的
    「重写为」才会在重扫之后落回同一行。

    命中映射的目标按「最深的那条映射键」聚类，组前缀就是映射键本身。
    不能先求公共前缀再反查映射：只有一个目标时前缀会退化成它的父目录，
    比映射键更浅，反查不到，「重写为」列就成了空白（实测过）。
    """
    mappings = {os.path.normpath(k): v for k, v in (mappings or {}).items()}

    matched: dict[str, int] = {}
    rest: list[str] = []
    for t in targets:
        norm = os.path.normpath(t)
        key = _best_mapping_key(norm, mappings)
        if key:
            matched[key] = matched.get(key, 0) + 1
        else:
            rest.append(norm)

    groups = [TargetGroup(prefix=key, count=count, mapped_to=mappings[key])
              for key, count in matched.items()]
    groups.extend(_group_by_prefix(rest, mappings))
    groups.sort(key=lambda g: g.prefix)
    return groups


def _group_by_prefix(targets: list[str],
                     mappings: dict[str, str]) -> list[TargetGroup]:
    """按盘符分组，再取组内最长公共前缀；前缀退化为盘根时改用各自父目录。"""
    def mapped_to(prefix: str) -> str:
        rewritten = apply_mappings(prefix, mappings)
        return "" if os.path.normcase(rewritten) == os.path.normcase(prefix) else rewritten

    by_drive: dict[str, list[str]] = {}
    for t in targets:
        by_drive.setdefault(os.path.splitdrive(t)[0].upper(), []).append(os.path.normpath(t))

    groups: list[TargetGroup] = []
    for drive, items in sorted(by_drive.items()):
        prefix = _common_prefix(items)
        root = drive + os.sep if drive else os.sep
        if len(items) > 1 and prefix.rstrip("\\/") in (drive, drive + os.sep, ""):
            buckets: dict[str, int] = {}
            for it in items:
                buckets[os.path.dirname(it)] = buckets.get(os.path.dirname(it), 0) + 1
            for parent, count in sorted(buckets.items()):
                groups.append(TargetGroup(prefix=parent, count=count,
                                          mapped_to=mapped_to(parent)))
        else:
            if len(items) == 1:
                prefix = os.path.dirname(items[0])
            groups.append(TargetGroup(prefix=prefix or root, count=len(items),
                                      mapped_to=mapped_to(prefix or root)))
    return groups


def create(link_path, target, is_dir: bool, prefer_junction: bool = True) -> str:
    """创建链接，返回实际类型。目录默认用 junction（不需要管理员权限）。"""
    if is_dir and prefer_junction:
        reparse.create_junction(link_path, target)
        return "junction"
    reparse.create_symlink(link_path, target, is_dir)
    return "symlink"


def _near(link_path, target: str) -> str:
    """相对路径按链接所在目录解析成绝对路径；绝对路径原样返回。

    相对链接在迁移时按原样保留，因此计划里的 new_target 也是相对值：
    不解析就直接比，任意一条相对链接都会被判成「指错了」。
    """
    if os.path.isabs(target):
        return target
    return os.path.join(os.path.dirname(str(link_path)), target)


def resolved_target(link_path) -> str:
    """链接实际指向的路径；读不出来返回 ""。

    只能走 readlink：junction 的目标可能压根不存在（断链），
    `exists` / `islink` 那套跟着链接走的判定在这种现场全是 False。
    """
    try:
        raw = os.readlink(str(link_path))
    except OSError:
        return ""
    return _near(link_path, norm_target(raw))


def points_to(link_path, wanted) -> bool:
    """这条链接是否已经指向计划要求的位置（大小写不敏感、按分量比较）。"""
    actual = resolved_target(link_path)
    if not actual:
        return False
    expected = _near(link_path, str(wanted))
    return (os.path.normcase(os.path.normpath(actual))
            == os.path.normcase(os.path.normpath(expected)))


def verify(link_path) -> bool:
    """链接存在、确实是重解析点、且目标可访问。"""
    try:
        st = os.lstat(link_path)
    except OSError:
        return False
    import stat as _stat
    if not (st.st_file_attributes & _stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    target = resolved_target(link_path)
    return bool(target) and os.path.exists(target)
