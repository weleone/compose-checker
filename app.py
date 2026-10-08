#!/usr/bin/env python3
"""Docker Compose YAML 检查工具 - Web 版（带 docker run 互转）"""
import os
import re
import shlex
import yaml
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "compose-checker-secret-change-me")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def check_compose(content):
    """检查 docker-compose yaml，返回 errors 和 warnings 列表"""
    errors = []
    warnings = []

    # 1. YAML 语法
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        errors.append(f"YAML 语法错误: {e}")
        return errors, warnings

    if not isinstance(data, dict):
        errors.append("顶层必须是 mapping（键值对），不是列表或标量")
        return errors, warnings

    # 2. services
    services = data.get("services")
    if not services:
        errors.append("缺少顶层 'services' 字段")
        return errors, warnings
    if not isinstance(services, dict):
        errors.append("'services' 必须是 mapping")
        return errors, warnings

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(f"服务 '{svc_name}' 的定义必须是 mapping")
            continue

        # image / build 必须有其一
        if "image" not in svc and "build" not in svc:
            errors.append(f"服务 '{svc_name}' 缺少 'image' 或 'build'")

        # image 检查
        image = svc.get("image", "")
        if image:
            if ":" not in image:
                warnings.append(f"服务 '{svc_name}' 的 image '{image}' 没写 tag，默认 latest")
            elif image.endswith(":latest"):
                warnings.append(f"服务 '{svc_name}' 用了 :latest 标签，生产环境建议 pin 具体版本")

        # restart
        if "restart" not in svc:
            warnings.append(f"服务 '{svc_name}' 没设 restart 策略（建议 unless-stopped）")

        # ports 格式
        for p in svc.get("ports", []) or []:
            s = str(p)
            if not re.match(r'^"?\d+(-\d+)?:\d+(-\d+)?(/\w+)?"?$', s) and not re.match(r'^".*:. *"$', s):
                # 宽松检查：只要包含冒号即可
                if ":" not in s:
                    warnings.append(f"服务 '{svc_name}' 的 ports '{s}' 格式可疑")

        # volumes 绝对路径提醒
        for v in svc.get("volumes", []) or []:
            s = str(v)
            if s.startswith("/") and ":" in s:
                host_path = s.split(":")[0]
                if not host_path.startswith(("./", "../")):
                    pass  # 绝对路径允许，不警告

        # container_name 重复检查在多文件场景，这里只做单文件
        env = svc.get("environment", {})
        if isinstance(env, list):
            for e in env:
                if isinstance(e, str) and "=" not in e and e.strip():
                    warnings.append(f"服务 '{svc_name}' 的 environment '{e}' 没有值")

    # 3. 顶层 networks / volumes 类型检查
    for key in ("networks", "volumes"):
        if key in data and data[key] is not None and not isinstance(data[key], dict):
            errors.append(f"顶层 '{key}' 必须是 mapping")

    # 4. version 字段（已废弃）
    if "version" in data:
        warnings.append("顶层 'version' 字段已废弃，新版 compose 会忽略它")

    return errors, warnings


# 已知的服务属性名，用于检测"漏了服务名"的错误
SERVICE_KEYS = {"image", "container_name", "ports", "volumes", "environment",
                "restart", "command", "entrypoint", "networks", "depends_on",
                "build", "env_file", "labels", "hostname", "user", "working_dir"}

def try_fix_missing_service_name(content):
    """如果 services 下直接写了服务属性（漏了服务名），尝试补上服务名。返回修正后的文本或 None"""
    lines = content.split("\n")
    # 找到 services: 这一行
    svc_idx = -1
    for i, line in enumerate(lines):
        if re.match(r'^services\s*:\s*$', line):
            svc_idx = i
            break
    if svc_idx < 0:
        return None
    # 检查 services 下的第一层 key 是否有服务属性名
    has_prop = False
    for line in lines[svc_idx + 1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = re.match(r'^(\s*)(\S+?)\s*:', line)
        if not m:
            continue
        indent = len(m.group(1))
        key = m.group(2)
        if indent == 0:  # 回到顶层，结束
            break
        if key in SERVICE_KEYS:
            has_prop = True
            break
        # 遇到正常的服务名（下一行缩进更深），说明结构正常
        break
    if not has_prop:
        return None
    # 用 container_name 的值做服务名，找不到就用 app
    svc_name = "app"
    for line in lines[svc_idx + 1:]:
        m = re.match(r'^\s*container_name\s*:\s*(\S+)', line)
        if m:
            svc_name = m.group(1).strip('"\'')
            break
    # 把 services 下的所有内容统一缩进一级，插入服务名
    out = lines[:svc_idx + 1]
    out.append(f"  {svc_name}:")
    for line in lines[svc_idx + 1:]:
        if line.strip() == "":
            out.append(line)
        elif re.match(r'^(\S)', line):
            out.append(line)  # 顶层 key 不动
        else:
            out.append("  " + line)
    return "\n".join(out)


def fix_colon_spacing(content):
    """修复冒号后缺空格的问题，如 DERP_DOMAIN:a.abcad.cn → DERP_DOMAIN: a.abcad.cn
    返回 (fixed_text, count)"""
    count = 0
    out = []
    for line in content.split("\n"):
        # 匹配行首缩进 + KEY:值（冒号后紧跟非空格、非冒号）
        # 排除 URL (http://)、时间 (12:30) 等
        m = re.match(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*):([^ :/][^:]*)(\s*(#.*)?)$', line)
        if m:
            indent, key, val, comment = m.group(1), m.group(2), m.group(3).strip(), m.group(4) or ""
            # 排除已经是 key: value 的（val 开头有空格的情况上面已排除）
            # 排除值里带 :// 的（URL）
            if "://" not in val:
                line = f"{indent}{key}: {val}{comment}"
                count += 1
        out.append(line)
    return "\n".join(out), count


def normalize_indent(content):
    """尽力规范缩进：服务属性归到4格，列表项归到6格。返回 (fixed_text, count)"""
    count = 0
    lines = content.split("\n")
    out = []
    in_services = False
    svc_indent = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        # 检测 services:
        if re.match(r'^services\s*:\s*$', line):
            in_services = True
            out.append(line)
            continue
        if not in_services:
            out.append(line)
            continue
        # 顶层 key，退出 services
        if re.match(r'^\S', line):
            in_services = False
            out.append(line)
            continue
        m = re.match(r'^(\s*)(- )?(\S+?)\s*:', line)
        # 先判断是不是列表项（- 开头）
        lm = re.match(r'^(\s*)-\s+', line)
        if lm:
            indent = len(lm.group(1))
            if indent != 6 and indent < 6:
                line = " " * 6 + stripped
                count += 1
            out.append(line)
            continue
        if not m:
            out.append(line)
            continue
        indent = len(m.group(1))
        is_list = m.group(2) is not None
        key = m.group(3)
        # 服务名：2格缩进、以:结尾、不是已知属性
        if indent == 2 and key not in SERVICE_KEYS and not is_list:
            svc_indent = 2
            out.append(line)
            continue
        # 已知服务属性，应该在4格
        if key in SERVICE_KEYS and not is_list:
            if indent != 4:
                line = " " * 4 + stripped
                count += 1
            out.append(line)
            continue
        out.append(line)
    return "\n".join(out), count


def fix_compose(content):
    """自动修正可修复的问题，返回 (fixed_yaml, applied_fixes列表)"""
    fixes = []
    # 预处理：修冒号后缺空格
    content, colon_n = fix_colon_spacing(content)
    if colon_n:
        fixes.append(f"修正了 {colon_n} 处冒号后缺空格")
    # 预处理：规范缩进
    content, indent_n = normalize_indent(content)
    if indent_n:
        fixes.append(f"修正了 {indent_n} 处缩进")
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        # 尝试修复"services 下直接写了服务属性、漏了服务名"的常见错误
        fixed = try_fix_missing_service_name(content)
        if fixed:
            data = yaml.safe_load(fixed)
            fixes.append("检测到 services 下漏了服务名，已自动补上")
        else:
            return None, [f"YAML 语法错误，无法自动修正: {e}"]
    if not isinstance(data, dict):
        return None, ["顶层不是 mapping，无法修正"]
    # 1. 去掉废弃的 version
    if "version" in data:
        del data["version"]
        fixes.append("删除了废弃的顶层 version 字段")
    # 2. 补 restart
    services = data.get("services", {})
    if isinstance(services, dict):
        for svc_name, svc in services.items():
            if isinstance(svc, dict) and "restart" not in svc:
                svc["restart"] = "unless-stopped"
                fixes.append(f"服务 '{svc_name}' 补了 restart: unless-stopped")
    if not fixes:
        return None, ["没有可自动修正的问题"]
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False), fixes


def docker_run_to_compose(run_cmd):
    """docker run 命令转 compose yaml"""
    run_cmd = run_cmd.strip()
    # 去掉开头的 docker run
    run_cmd = re.sub(r'^docker\s+run\s+', '', run_cmd)
    # 处理续行符
    run_cmd = run_cmd.replace("\\\n", " ").replace("\\", " ")
    try:
        parts = shlex.split(run_cmd)
    except ValueError as e:
        return None, f"命令解析失败: {e}"

    svc = {}
    name = "app"
    image = None
    cmd_args = []
    i = 0
    # 先找到 image（第一个非 flag 参数）
    # 简化：遍历解析
    positional = []
    while i < len(parts):
        p = parts[i]
        if p in ("-d", "--detach", "-it", "-i", "-t", "--rm"):
            i += 1
        elif p == "--name" and i + 1 < len(parts):
            name = parts[i + 1]; i += 2
        elif p.startswith("--name="):
            name = p.split("=", 1)[1]; i += 1
        elif p in ("-p", "--publish") and i + 1 < len(parts):
            svc.setdefault("ports", []).append(parts[i+1]); i += 2
        elif p in ("-v", "--volume") and i + 1 < len(parts):
            svc.setdefault("volumes", []).append(parts[i+1]); i += 2
        elif p in ("-e", "--env") and i + 1 < len(parts):
            svc.setdefault("environment", []).append(parts[i+1]); i += 2
        elif p == "--restart" and i + 1 < len(parts):
            svc["restart"] = parts[i+1]; i += 2
        elif p.startswith("--restart="):
            svc["restart"] = p.split("=", 1)[1]; i += 1
        elif p == "--network" and i + 1 < len(parts):
            svc.setdefault("networks", []).append(parts[i+1]); i += 2
        elif p in ("-w", "--workdir") and i + 1 < len(parts):
            svc["working_dir"] = parts[i+1]; i += 2
        elif p in ("-u", "--user") and i + 1 < len(parts):
            svc["user"] = parts[i+1]; i += 2
        elif p == "--hostname" and i + 1 < len(parts):
            svc["hostname"] = parts[i+1]; i += 2
        elif p == "--privileged":
            svc["privileged"] = True; i += 1
        elif p == "--cap-add" and i + 1 < len(parts):
            svc.setdefault("cap_add", []).append(parts[i+1]); i += 2
        elif p == "--cap-drop" and i + 1 < len(parts):
            svc.setdefault("cap_drop", []).append(parts[i+1]); i += 2
        elif p == "--device" and i + 1 < len(parts):
            svc.setdefault("devices", []).append(parts[i+1]); i += 2
        elif p == "--dns" and i + 1 < len(parts):
            svc.setdefault("dns", []).append(parts[i+1]); i += 2
        elif p == "--entrypoint" and i + 1 < len(parts):
            svc["entrypoint"] = parts[i+1]; i += 2
        elif p.startswith("-"):
            # 未知 flag，跳过（带值的尝试跳过下一个）
            i += 1
        else:
            positional.append(p)
            i += 1

    if not positional:
        return None, "没找到 image"
    image = positional[0]
    if len(positional) > 1:
        svc["command"] = " ".join(positional[1:])

    svc["image"] = image
    svc["container_name"] = name

    # 排序：image 放前面
    ordered = {"image": svc.pop("image")}
    if "container_name" in svc:
        ordered["container_name"] = svc.pop("container_name")
    ordered.update(svc)

    result = {"services": {name: ordered}}
    return yaml.safe_dump(result, allow_unicode=True, sort_keys=False), None


def compose_to_docker_run(content):
    """compose yaml 转 docker run 命令"""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return None, f"YAML 解析失败: {e}"
    if not isinstance(data, dict) or "services" not in data:
        return None, "不是有效的 compose 文件"
    services = data["services"]
    cmds = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        parts = ["docker run -d"]
        cname = svc.get("container_name", svc_name)
        parts.append(f"--name {shlex.quote(cname)}")
        for p in svc.get("ports", []) or []:
            parts.append(f"-p {shlex.quote(str(p).strip(chr(34)))}")
        for v in svc.get("volumes", []) or []:
            parts.append(f"-v {shlex.quote(str(v))}")
        env = svc.get("environment", {})
        if isinstance(env, dict):
            for k, v in env.items():
                parts.append(f"-e {shlex.quote(f'{k}={v}')}")
        elif isinstance(env, list):
            for e in env:
                parts.append(f"-e {shlex.quote(str(e))}")
        if "restart" in svc:
            parts.append(f"--restart {shlex.quote(str(svc['restart']))}")
        for n in svc.get("networks", []) or []:
            parts.append(f"--network {shlex.quote(str(n))}")
        if "working_dir" in svc:
            parts.append(f"-w {shlex.quote(str(svc['working_dir']))}")
        if "user" in svc:
            parts.append(f"-u {shlex.quote(str(svc['user']))}")
        if "hostname" in svc:
            parts.append(f"--hostname {shlex.quote(str(svc['hostname']))}")
        if svc.get("privileged"):
            parts.append("--privileged")
        for c in svc.get("cap_add", []) or []:
            parts.append(f"--cap-add {shlex.quote(str(c))}")
        for c in svc.get("cap_drop", []) or []:
            parts.append(f"--cap-drop {shlex.quote(str(c))}")
        for d in svc.get("devices", []) or []:
            parts.append(f"--device {shlex.quote(str(d))}")
        if "entrypoint" in svc:
            parts.append(f"--entrypoint {shlex.quote(str(svc['entrypoint']))}")
        image = svc.get("image", "")
        if not image and "build" in svc:
            image = f"# [build: {svc['build']}] <image>"
        parts.append(shlex.quote(str(image)))
        if "command" in svc:
            cmd = svc["command"]
            parts.append(cmd if isinstance(cmd, str) else " ".join(map(str, cmd)))
        cmds.append((svc_name, " ".join(parts)))
    if not cmds:
        return None, "没有找到可转换的服务"
    return cmds, None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pwd = request.form.get("password", "")
        if user == ADMIN_USER and pwd == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="用户名或密码错误")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    content = ""
    errors = []
    warnings = []
    filename = ""

    if request.method == "POST":
        # 文件上传优先
        f = request.files.get("file")
        if f and f.filename:
            filename = f.filename
            content = f.read().decode("utf-8", errors="replace")
        else:
            content = request.form.get("content", "")
            filename = "pasted-content"

        if content.strip():
            errors, warnings = check_compose(content)
        else:
            errors.append("内容为空")

    return render_template("index.html", content=content, errors=errors,
                           warnings=warnings, filename=filename)


@app.route("/api/check", methods=["POST"])
@login_required
def api_check():
    data = request.get_json(silent=True) or {}
    content = data.get("content", "")
    errors, warnings = check_compose(content)
    return jsonify({"errors": errors, "warnings": warnings})


@app.route("/api/run2compose", methods=["POST"])
@login_required
def api_run2compose():
    data = request.get_json(silent=True) or {}
    result, err = docker_run_to_compose(data.get("cmd", ""))
    if err:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "yaml": result})


@app.route("/api/compose2run", methods=["POST"])
@login_required
def api_compose2run():
    data = request.get_json(silent=True) or {}
    result, err = compose_to_docker_run(data.get("content", ""))
    if err:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "commands": [{"service": n, "cmd": c} for n, c in result]})


@app.route("/api/fix", methods=["POST"])
@login_required
def api_fix():
    data = request.get_json(silent=True) or {}
    result, fixes = fix_compose(data.get("content", ""))
    if result is None:
        return jsonify({"ok": False, "error": "; ".join(fixes)})
    return jsonify({"ok": True, "yaml": result, "fixes": fixes})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
