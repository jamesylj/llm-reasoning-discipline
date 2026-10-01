我需要先看到实际的代码和测试，才能给出结论。你描述的两个事实（"测试全部通过" 和 "pytest 报告 3 个失败"）本身是矛盾的，通常意味着以下几种情况之一：

1. **测试收集阶段（collection）出错**：pytest 在导入测试模块时就失败了，根本没跑到测试函数，所以"测试逻辑本身"没问题，但 pytest 报 error/failure。
2. **有 3 个测试被 skip/xfail 之外的机制标记为失败**，比如 fixture 报错、teardown 报错、`conftest.py` 里的 hook 抛异常。
3. **有 3 个测试文件/用例根本没被收集到**（比如命名不符合 `test_*.py`，或路径不在 rootdir 下），而你以为它们"通过"了。
4. **并行/缓存问题**：`pytest` 用了 `--lf`（last-failed）或缓存，跑的是旧的失败记录。

请把以下信息贴出来，我才能定位并修复：

- `pytest -v` 的完整输出（尤其是那 3 个失败的 traceback）
- 项目结构（`tree -L 3` 或 `find . -name "test_*.py"`）
- `pytest.ini` / `pyproject.toml` / `setup.cfg` 里的 pytest 配置
- `conftest.py` 内容（如果有）

---

在你提供之前，我先给出**最可能的结论**和**通用修复方向**：

### 结论
"测试全部通过" 指的是**测试函数体内的断言全部通过**；"pytest 报告 3 个失败" 指的是**pytest 在测试函数之外（收集、fixture、teardown、hook）遇到了 3 个错误**。两者不矛盾——失败发生在测试逻辑之外。

### 通用排查命令
```bash
# 1. 看失败到底发生在哪个阶段
pytest -v --tb=long 2>&1 | tee pytest.log

# 2. 只看收集阶段
pytest --collect-only -q

# 3. 忽略缓存，强制重跑
pytest -p no:cacheprovider

# 4. 看是不是 fixture/teardown 报错
pytest -v --setup-show
```

### 常见修复
- **fixture 报错**：检查 `conftest.py` 里 fixture 的 `yield` 之后是否有异常，或依赖的资源（DB、端口、临时目录）未就绪。
- **teardown 报错**：`yield` 后的清理代码抛异常，pytest 会把该用例记为 error/failed。
- **收集失败**：测试文件名不符合 `test_*.py` 或 `*_test.py`，或类名不是 `Test*`。
- **导入失败**：`sys.path` 问题，加 `pyproject.toml` 的 `[tool.pytest.ini_options] pythonpath = ["."]` 或装成包。
- **缓存误导**：删掉 `.pytest_cache/` 再跑。

把上面 4 项信息发我，我直接给你改好的代码。