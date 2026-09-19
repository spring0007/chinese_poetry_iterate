# -*- coding: utf-8 -*-
"""生成 PyCharm 的 .idea/runConfigurations/*.xml，省掉手工配 5 个入口的麻烦。

放在 poetry-pipeline/.idea 而不是项目根的 .idea：源库根目录有自己的 .idea（git 仓库自带），
往里写文件会污染别人的仓库；这里只服务于本工程。

路径一律用 $PROJECT_DIR$/poetry-pipeline/...，对应「PyCharm 打开 E:\\chinese-poetry-master
作为项目根」的用法（README §零 有说明）。若直接把 poetry-pipeline 当项目根，
这些配置的路径会失效，改为手工指定即可。
"""
from __future__ import annotations
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".idea", "runConfigurations")
CFG = "$PROJECT_DIR$/poetry-pipeline"

# name, script(相对工程), parameters
ITEMS = [
    ("1 Build 全量", "build.py", "--with-strains"),
    ("2 Verify 校验", "verify_db.py", ""),
    ("3 Query 基准", "query.py", "bench"),
    ("4 App 桌面窗口", "app.py", ""),
    ("5 API 服务", "examples/api_server.py", "--allow-write --port 8787"),
    ("6 Build 冒烟", "build.py", "--out ./dist-smoke --limit 3000"),
]

TPL = """<component name="ProjectRunConfigurationManager">
  <configuration default="false" name="{name}" type="PythonConfigurationType" factoryName="Python" nameIsGenerated="false">
    <option name="INTERPRETER_OPTIONS" value="" />
    <option name="PARENT_ENVS" value="true" />
    <option name="SDK_HOME" value="{cfg}/.venv/Scripts/python.exe" />
    <option name="WORKING_DIRECTORY" value="{cfg}" />
    <option name="IS_MODULE_SDK" value="false" />
    <option name="ADD_CONTENT_ROOTS" value="true" />
    <option name="ADD_SOURCE_ROOTS" value="true" />
    <option name="SCRIPT_NAME" value="{cfg}/{script}" />
    <option name="PARAMETERS" value="{params}" />
    <option name="SHOW_COMMAND_LINE" value="false" />
    <option name="EMULATE_TERMINAL" value="false" />
    <option name="MODULE_MODE" value="false" />
    <option name="REDIRECT_INPUT" value="false" />
    <option name="ELEVATE" value="false" />
    <method v="2" />
  </configuration>
</component>
"""

os.makedirs(OUT, exist_ok=True)
for name, script, params in ITEMS:
    fn = name.replace(" ", "_") + ".xml"
    with open(os.path.join(OUT, fn), "w", encoding="utf-8") as f:
        f.write(TPL.format(name=name, cfg=CFG, script=script, params=params))
    print("写入", fn)
print("目录:", OUT)
