import asyncio
import os
from typing import TYPE_CHECKING, cast

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

if TYPE_CHECKING:
    from astrbot.core.star.star_manager import PluginManager

PULL_ARGS = {
    "ff-only": ("pull", "--ff-only"),
    "merge": ("pull", "--no-rebase"),
    "rebase": ("pull", "--rebase"),
}


class Main(Star):
    """拉取并热重载 git 管理的插件"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._self_dir = os.path.basename(os.path.dirname(os.path.abspath(__file__)))

    @property
    def _sm(self) -> "PluginManager":
        return cast("PluginManager", self.context._star_manager)

    # ---- git 调用 ----
    async def _git(self, path: str, *args: str) -> tuple[int, str, str]:
        """执行 git 命令,返回 (returncode, stdout, stderr)。"""
        timeout = int(self.config.get("git_timeout", 120))
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                path,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return -1, "", "未找到 git 命令"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"git 操作超时(>{timeout}s)"
        rc = proc.returncode if proc.returncode is not None else -1
        return (
            rc,
            out.decode(errors="replace").strip(),
            err.decode(errors="replace").strip(),
        )

    # ---- 发现 git 插件 ----
    def _discover(self) -> list[dict]:
        """扫描插件目录,返回含 .git 的候选(已排除自身与配置的 exclude)。"""
        store = self._sm.plugin_store_path
        stars_by_dir = {
            s.root_dir_name: s for s in self.context.get_all_stars() if s.root_dir_name
        }
        exclude = {self._self_dir, *self.config.get("exclude", [])}
        result = []
        for name in sorted(os.listdir(store)):
            path = os.path.join(store, name)
            if not os.path.isdir(os.path.join(path, ".git")):
                continue
            if name in exclude:
                continue
            result.append({"dir": name, "path": path, "star": stars_by_dir.get(name)})
        return result

    @staticmethod
    def _names(item: dict) -> list[str]:
        keys = [item["dir"]]
        star = item["star"]
        if star and star.name:
            keys.append(star.name)
        return keys

    # ---- 单插件同步 ----
    async def _sync_one(self, item: dict) -> str:
        dirn, path, star = item["dir"], item["path"], item["star"]
        sm = self._sm

        rc, old, err = await self._git(path, "rev-parse", "HEAD")
        if rc != 0:
            return f"❌ {dirn}: 读取版本失败({err or 'rev-parse'})"

        rc, out, err = await self._git(path, *PULL_ARGS[self._pull_mode()])
        if rc != 0:
            return f"❌ {dirn}: pull 失败\n{(err or out)[:300]}"

        _, new, _ = await self._git(path, "rev-parse", "HEAD")
        if new == old:
            return f"✅ {dirn}: 已是最新({old[:7]})"

        ver = f"{old[:7]}→{new[:7]}"

        if self.config.get("reinstall_deps", True):
            try:
                await sm._ensure_plugin_requirements(path, star.name if star else dirn)
            except Exception as e:
                logger.error(f"[GitPluginSync] {dirn} 依赖安装失败", exc_info=True)
                return f"⚠ {dirn}: 已更新 {ver},但依赖安装失败,未重载({e})"

        try:
            if star and star.name:
                ok, rerr = await sm.reload(star.name)
            else:
                ok, rerr = await sm.load(specified_dir_name=dirn)
        except Exception as e:
            logger.error(f"[GitPluginSync] {dirn} 重载异常", exc_info=True)
            return f"⚠ {dirn}: 已更新 {ver},但重载异常({e})"

        if ok:
            return f"🔄 {dirn}: 已更新并重载 {ver}"
        return f"⚠ {dirn}: 已更新 {ver},但重载失败({rerr})"

    def _pull_mode(self) -> str:
        mode = self.config.get("pull_mode", "ff-only")
        return mode if mode in PULL_ARGS else "ff-only"

    # ---- 指令 ----
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("gitsync")
    async def gitsync(self, event: AstrMessageEvent, arg: str = ""):
        """管理 git 插件。用法: /gitsync(列出) | /gitsync all(全部更新) | /gitsync <插件名>"""
        arg = arg.strip()
        candidates = self._discover()

        if not candidates:
            yield event.plain_result("未发现任何含 .git 的插件。")
            return

        # /gitsync 或 /gitsync list —— 默认只列出,不拉取
        if arg == "" or arg.lower() == "list":
            lines = ["📋 git 插件(/gitsync all 更新全部):"]
            for c in candidates:
                _, branch, _ = await self._git(
                    c["path"], "rev-parse", "--abbrev-ref", "HEAD"
                )
                _, sha, _ = await self._git(c["path"], "rev-parse", "--short", "HEAD")
                loaded = "已加载" if c["star"] else "未加载"
                lines.append(f"• {c['dir']} [{branch or '?'}@{sha or '?'}] {loaded}")
            yield event.plain_result("\n".join(lines))
            return

        # /gitsync all —— 拉取并重载(仅重载有更新的)
        if arg.lower() == "all":
            yield event.plain_result(f"开始同步 {len(candidates)} 个 git 插件…")
            results = [await self._sync_one(c) for c in candidates]
            yield event.plain_result("\n".join(results))
            return

        # /gitsync <名字> —— 模糊 + 忽略大小写
        q = arg.lower()
        exact = [c for c in candidates if any(k.lower() == q for k in self._names(c))]
        matched = exact or [
            c for c in candidates if any(q in k.lower() for k in self._names(c))
        ]

        if not matched:
            yield event.plain_result(f"未找到匹配的 git 插件: {arg}")
            return
        if len(matched) > 1:
            names = "\n".join(f"• {c['dir']}" for c in matched)
            yield event.plain_result(f"匹配到多个插件,请用更精确的名字:\n{names}")
            return

        yield event.plain_result(f"开始同步 {matched[0]['dir']}…")
        yield event.plain_result(await self._sync_one(matched[0]))
