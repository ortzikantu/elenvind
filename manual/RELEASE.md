# 发布

## 两个仓库的分工

| 仓库 | 角色 |
|---|---|
| **Codeberg**（`codeberg.org/ortzikantu/elenvind`） | **主仓库**：代码、Issue、PR 都在这里；`pyproject.toml` 里的 Homepage / Repository / Issues 指向它 |
| **GitHub**（`github.com/ortzikantu/elenvind`） | **镜像 + 构建仓**：由 Codeberg 的内置镜像功能同步（含 tag），并负责跑 CI 与发布 PyPI、建 GitHub Release |

发布流水线只跑在 GitHub 上（PyPI 的 Trusted Publishing 需要 GitHub/GitLab 这类 OIDC 提供方）。
代码仍然只在 Codeberg 提交，推上去之后由镜像把 tag 同步到 GitHub 触发发布。

## 版本号只有一个来源

`pyproject.toml` 的 `[project].version`。`elenvind/core/version.py` 已安装时读
`importlib.metadata`，源码检出时解析 pyproject；`__version__` 与页脚显示的版本都跟着它。
**发版只需改这一处**，然后打一个同名 tag。

## 标准流程

```bash
# 0) 先确认工作树干净、测试全绿
.venv/bin/python -m unittest discover -s tests -t .     # 196 tests OK
.venv/bin/ruff check .

# 1) 升版本（唯一需要改的文件）
$EDITOR pyproject.toml          # version = "1.0.1"

# 2) 提交并推到主仓库（Codeberg）
git add -A && git commit -m "chore: release 1.0.1"
git push origin main

# 3) 打 tag 并推送 —— 镜像同步到 GitHub 后自动触发 publish 流水线
git tag v1.0.1
git push origin v1.0.1
```

流水线（`.github/workflows/publish.yml`）会自动做四件事：

1. **校验 tag 与 pyproject 版本一致**（`v1.0.1` ↔ `1.0.1`），不一致立刻失败；
2. 跑 `ruff check .` 与全套 196 项测试；
3. `python -m build` 产出 wheel + sdist，`twine check` 校验元数据，并断言产物里**没有**
   `config.toml` / `articles/` / `custom/` / `data/`（站点实例数据不入包）；
4. 用 OIDC 发布到 PyPI，并在 GitHub 建 Release、附上两个产物。

## 首次发布前：在 PyPI 登记 pending trusted publisher

Trusted Publishing 不需要在仓库里存任何 token，但**必须先登记**，否则流水线会以
`invalid-publisher` 失败。

1. 注册 / 登录 [pypi.org](https://pypi.org)（并开启 2FA）。
2. 打开 <https://pypi.org/manage/account/publishing/>，在 *Pending publishers* 里填：

| 字段 | 值 |
|---|---|
| PyPI Project Name | `elenvind` |
| Owner | `ortzikantu` |
| Repository name | `elenvind` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

3. 保存后，第一次成功发布时这个 pending publisher 会自动转成该项目的正式 publisher。
   之后要改字段，在项目页的 *Publishing* 里改。

`publish.yml` 里的 `environment: pypi` 必须与上面登记的 Environment name 一致；
GitHub 仓库的 Settings → Environments 里可以给 `pypi` 加人工审批或保护规则。

## 手工发布（备用路径）

流水线之外，也可以在本机发布（例如流水线临时不可用）。构建工具不进入运行时依赖：

```bash
python3 -m venv .venv-build && .venv-build/bin/pip install -U pip build twine
rm -rf dist build ./*.egg-info
.venv-build/bin/python -m build
.venv-build/bin/twine check dist/*
```

**先在 TestPyPI 演练**（两个站点账号独立，需单独建 token）：

```bash
.venv-build/bin/twine upload --repository testpypi dist/*
python3 -m venv /tmp/t
/tmp/t/bin/pip install -i https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ "elenvind[gunicorn]"
/tmp/t/bin/elenvind --version
```

正式发布（凭据用环境变量或 `~/.pypirc`，**不要**写进仓库）：

```bash
export TWINE_USERNAME=__token__
read -rs TWINE_PASSWORD && export TWINE_PASSWORD     # 粘贴 pypi-AgEI… 后回车
.venv-build/bin/twine upload dist/*
```

## 发布后核对

```bash
curl -s https://pypi.org/pypi/elenvind/json | python3 -c \
  "import json,sys; i=json.load(sys.stdin)['info']; print(i['name'], i['version'], i['requires_python'])"

python3 -m venv /tmp/prod
/tmp/prod/bin/pip install "elenvind[gunicorn]"
/tmp/prod/bin/elenvind doctor
```

同时看一眼：PyPI 页面 README 是否渲染正常、Homepage / Repository / Issues 是否指向
Codeberg、`Requires-Python`（≥3.11）与许可（GPL-3.0-only）是否正确；
GitHub 上是否生成了对应的 Release 与两个附件。

## 失败对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `invalid-publisher` / `trusted publishing` 相关报错 | PyPI 上没有登记 pending publisher，或字段与 workflow 不符 | 按上面的表格逐项核对（Owner / Repository / Workflow / Environment） |
| `tag 'v1.0.1' 与 pyproject 的 version '1.0.0' 不一致` | 忘了升版本，或 tag 打错 | 改 `pyproject.toml` 后重新打 tag |
| `400 File already exists` | 该版本已经发布过 | PyPI **不允许覆盖**：升版本再发（或对旧版本做 yank） |
| `400 License classifiers have been superseded…` | 同时写了 license 表达式与旧 `License ::` 分类器 | 本项目只保留表达式（PEP 639） |
| `400 The description failed to render` | README 里有 PyPI 不支持的写法 | 先 `twine check dist/*`；链接用绝对 URL |
| 产物检查步骤失败 | 站点实例数据混进了 wheel/sdist | 检查 `MANIFEST.in` 与 `[tool.setuptools.package-data]` |
| 镜像里没看到 tag | Codeberg 的镜像同步还没跑完 | 等同步完成或手动触发一次镜像；GitHub 上的流水线只认已同步的 tag |

## 版本与撤回规则

- **已发布的文件不可删除**，只能 *yank*（`pip` 默认跳过被 yank 的版本，但已有 lockfile 仍能装到）。
  要"修"一个已发布的版本，只能发一个新版本。
- 预发布版本用 PEP 440 形式：`1.0.1rc1`、`1.1.0.dev1`、`1.0.1.post1`。
  普通 `pip install` 默认跳过预发布，需要 `--pre`。
- tag 名 = `v` + `pyproject` 的 version（例如 version `1.0.1rc1` → tag `v1.0.1rc1`），
  这样流水线的校验才能对齐。

## 本地检查清单（提交 tag 之前）

```bash
.venv/bin/ruff check .
.venv/bin/python -m unittest discover -s tests -t .     # 196 tests OK
grep -n '^version' pyproject.toml                       # 与将要打的 tag 一致
```

`.github/workflows/ci.yml` 会在每次 push / PR 上跑同样的门禁（Python 3.11–3.14 矩阵），
外加一次"启动 + 探活"冒烟，所以镜像仓的绿灯就是发布前的最后一道确认。
