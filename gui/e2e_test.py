"""端到端联调测试（python gui/e2e_test.py）

全流程走一遍四个页面：
    数据页下载 1 分钟行情（真实数据源，有缓存走缓存）
    → 参数寻优（1 分钟线网格搜索）
    → 「用最优参数回测」一键应用到回测页并自动运行（真实 1 分钟回测）
    → 批量回测（1 分钟线 × 2 只股票）+ 自动推送汇总

说明：
  - 行情下载需要联网（数据源：新浪行情），通知渠道全程 mock，绝不真实发消息；
  - 所有弹窗（QMessageBox）都被 mock，防止离屏环境下卡住测试；
  - 每个环节都有超时保护，失败时会给出清晰的等待超时提示。
"""
import os
import sys

# 保证直接运行本文件时能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock

from PyQt6.QtCore import QDate, QEventLoop, QTimer, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox


def _wait_until(cond, timeout_ms, desc):
    """在事件循环里每 200ms 轮询 cond()，直到为真；超时直接报错"""
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(200)

    def check():
        if cond():
            loop.quit()

    timer.timeout.connect(check)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    assert cond(), f"等待超时（{timeout_ms // 1000} 秒）：{desc}"


def main():
    from gui.main_window import MainWindow, apply_style

    app = QApplication.instance() or QApplication(sys.argv)
    apply_style(app)
    win = MainWindow()

    # 最近 6 个自然日（新浪分钟线只提供约 5 个交易日，够用）
    d_start = QDate.currentDate().addDays(-6)
    d_end = QDate.currentDate()

    # 全部弹窗 mock 掉：测试里不能有阻塞式对话框
    with mock.patch.object(QMessageBox, "information"), \
            mock.patch.object(QMessageBox, "warning"), \
            mock.patch.object(QMessageBox, "critical"):

        # ===== ① 数据页：下载 1 分钟行情 =====
        dp = win.data_page
        dp.code_edit.setText("600519")
        dp.start_edit.setDate(d_start)
        dp.end_edit.setDate(d_end)
        dp.freq_combo.setCurrentIndex(dp.freq_combo.findData("1min"))
        dp._on_download()
        _wait_until(lambda: dp._last_df is not None and not dp._last_df.empty,
                    120_000, "数据页下载 1 分钟行情")
        df = dp._last_df
        assert dp._last_freq == "1min"
        assert len(df) > 100, f"1 分钟 K 线数量异常：{len(df)}"
        assert not df[["open", "high", "low", "close"]].isna().any().any(), \
            "分钟线不应有空的 OHLC"
        print(f"① 数据页下载 1 分钟行情：{len(df)} 根 K 线，"
              f"{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")

        # ===== ② 参数寻优：1 分钟线网格搜索（2×2 组合，快） =====
        op = win.optimize_page
        op.code_edit.setText("600519")
        op.start_edit.setDate(d_start)
        op.end_edit.setDate(d_end)
        op.freq_combo.setCurrentIndex(op.freq_combo.findData("1min"))
        op.method_combo.setCurrentIndex(op.method_combo.findData("grid"))
        op.workers_spin.setValue(1)
        for key, lo, hi in (("fast", 2, 3), ("slow", 5, 6)):
            ws = op._range_widgets[key]
            ws["min"].setValue(lo)
            ws["max"].setValue(hi)
            ws["step"].setValue(1)
        op._on_run()
        _wait_until(lambda: op.apply_btn.isEnabled(), 180_000,
                    "参数寻优完成（1 分钟线网格搜索）")
        best = op._out["best_params"]
        assert best, "寻优结果不应为空"
        print(f"② 参数寻优（1 分钟线网格）：最优 {best}，"
              f"{op._out['metric']} = {op._out['best_value']:.4f}")

        # ===== ③ 一键应用最优参数 → 回测页自动运行（真实 1 分钟回测） =====
        bp = win.backtest_page
        bp.auto_push_check.setChecked(False)  # 推送验证统一放在批量回测一步
        op._apply_best()
        assert win.tabs.currentWidget() is bp, "应用后应自动切到单股回测页"
        assert bp.freq_combo.currentData() == "1min"
        for k, v in best.items():
            assert bp._param_widgets[k].value() == v, f"参数 {k} 未同步到回测页"
        _wait_until(lambda: bp.push_btn.isEnabled(), 180_000,
                    "一键应用后自动回测完成")
        assert bp._last_info.get("频率") == "1 分钟", bp._last_info
        assert bp._last_info.get("code") == "600519"
        print(f"③ 一键应用最优参数：自动回测完成，"
              f"策略 {bp._last_info.get('策略')}，频率 {bp._last_info.get('频率')}")

        # ===== ④ 批量回测（1 分钟线）+ 自动推送汇总 =====
        bat = win.batch_page
        bat.codes_edit.setPlainText("600519\n000001")
        bat.start_edit.setDate(d_start)
        bat.end_edit.setDate(d_end)
        bat.freq_combo.setCurrentIndex(bat.freq_combo.findData("1min"))
        bat.workers_spin.setValue(1)
        # 只勾第一个策略（双均线），加快速度
        for i in range(bat.strategy_list.count()):
            item = bat.strategy_list.item(i)
            item.setCheckState(Qt.CheckState.Checked if i == 0
                               else Qt.CheckState.Unchecked)
        bat.auto_push_check.setChecked(True)

        with mock.patch("notification.notifier.Notifier") as MockNotifier:
            MockNotifier.return_value.send_backtest_finished.return_value = \
                {"邮件": True, "企业微信": True}
            bat._on_run()
            _wait_until(
                lambda: MockNotifier.return_value.send_backtest_finished.called,
                300_000, "批量回测完成并自动推送汇总")
        summary = MockNotifier.return_value.send_backtest_finished.call_args[0][0]
        assert summary["任务类型"] == "批量回测", summary
        assert summary["数据频率"] == "1 分钟", summary
        ok_count = int(summary["成功组合数"].split()[0])
        assert ok_count >= 1, f"批量回测至少应有 1 个成功组合：{summary}"
        if bat._out["failed"]:
            print(f"    （跳过失败组合：{bat._out['failed']}）")
        print(f"④ 批量回测（1 分钟线）+ 自动推送：成功 {summary['成功组合数']}，"
              f"最优 {summary['最优组合（按夏普比率）']}")
        assert len(bat._out["results"]) == ok_count

    win.close()
    print("===== 端到端联调测试全部通过 ====="
          "（下载分钟数据 → 参数优化 → 一键回测 → 批量回测 → 推送通知）")


if __name__ == "__main__":
    main()
