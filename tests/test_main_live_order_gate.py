from __future__ import annotations

import unittest
from unittest.mock import patch

import main
from delta_rest_client import OrderType


class LiveOrderGateTests(unittest.TestCase):
    def setUp(self):
        self.previous_execution_halt = main.EXECUTION_HALT
        main.EXECUTION_HALT = False

    def tearDown(self):
        main.EXECUTION_HALT = self.previous_execution_halt

    def test_entry_is_blocked_by_default_without_exchange_request(self):
        with (
            patch.object(main, "ENABLE_LIVE_ORDERS", False),
            patch.object(main.client, "request") as request,
        ):
            self.assertIsNone(main.place_entry("123", 1, 100.0))
        request.assert_not_called()

    def test_entry_can_be_armed_with_mocked_exchange_response(self):
        with (
            patch.object(main, "ENABLE_LIVE_ORDERS", True),
            patch.object(main.client, "request") as request,
        ):
            request.return_value.json.return_value = {
                "success": True,
                "result": {"id": "mock-order"},
            }
            response = main.place_entry("123", 1, 100.0)
        self.assertEqual(response["result"]["id"], "mock-order")
        request.assert_called_once()

    def test_execute_trade_does_not_prompt_or_authenticate_when_disarmed(self):
        with (
            patch.object(main, "ENABLE_LIVE_ORDERS", False),
            patch.object(main, "is_authenticated") as authenticated,
            patch.object(main, "wait_for_telegram_confirmation") as confirmation,
        ):
            self.assertIsNone(
                main.execute_trade(
                    "123",
                    "TESTUSD",
                    {"quantity": 1},
                    100.0,
                    1.0,
                )
            )
        authenticated.assert_not_called()
        confirmation.assert_not_called()

    def test_execution_halt_persists_to_state_and_blocks_future_entries(self):
        state = {}
        with patch.object(main, "save_state") as save:
            main.halt_live_entries("uncertain order", state)
        self.assertTrue(main.EXECUTION_HALT)
        self.assertTrue(state["execution_halt"])
        self.assertEqual(state["execution_halt_reason"], "uncertain order")
        save.assert_called_once_with(state)
        with (
            patch.object(main, "ENABLE_LIVE_ORDERS", True),
            patch.object(main, "is_authenticated") as authenticated,
        ):
            self.assertIsNone(
                main.execute_trade("123", "TESTUSD", {}, 100.0, 1.0, state=state)
            )
        authenticated.assert_not_called()

    def test_stop_order_uses_sdk_enum_and_returns_direct_order_record(self):
        with patch.object(main.client, "place_stop_order", return_value={"id": "sl-1"}) as place:
            response = main.place_sl("123", 2, 101.0)
        self.assertEqual(response, {"id": "sl-1"})
        place.assert_called_once_with(
            product_id="123",
            size=2,
            side="buy",
            order_type=OrderType.MARKET,
            stop_price="101.0",
        )

    def test_bracket_uses_order_id_from_direct_stop_response(self):
        with (
            patch.object(main, "place_sl", return_value={"id": "sl-1"}),
            patch.object(main, "place_tp", return_value={
                "success": True,
                "result": {"id": "tp-1"},
            }) as take_profit,
        ):
            bracket = main.place_brackets("123", 4, 100.0, 101.0)
        self.assertEqual(bracket["sl_id"], "sl-1")
        self.assertEqual(bracket["tp_id"], "tp-1")
        take_profit.assert_called_once()

    def test_bracket_does_not_place_take_profit_without_confirmed_stop(self):
        with (
            patch.object(main, "place_sl", return_value=None),
            patch.object(main, "place_tp") as take_profit,
        ):
            bracket = main.place_brackets("123", 4, 100.0, 101.0)
        self.assertIsNone(bracket["sl_id"])
        self.assertIn("error", bracket)
        take_profit.assert_not_called()

    def test_wait_fill_reports_exact_full_and_terminal_partial_fills(self):
        with (
            patch.object(main.client, "get_order_by_id", return_value={
                "size": 4,
                "unfilled_size": 0,
                "state": "closed",
            }),
            patch.object(main.time, "sleep"),
        ):
            self.assertEqual(main.wait_fill("123", "order-1", 4, timeout=1), 4.0)
        with (
            patch.object(main.client, "get_order_by_id", return_value={
                "size": 4,
                "unfilled_size": 2,
                "state": "cancelled",
            }),
            patch.object(main.time, "sleep"),
        ):
            self.assertEqual(main.wait_fill("123", "order-2", 4, timeout=1), 2.0)

    def test_wait_fill_timeout_cancels_then_confirms_partial_fill(self):
        with (
            patch.object(main.client, "request") as request,
            patch.object(main.client, "get_order_by_id", return_value={
                "size": 4,
                "unfilled_size": 3,
                "state": "cancelled",
            }),
        ):
            request.return_value.json.return_value = {"success": True}
            self.assertEqual(main.wait_fill("123", "order-3", 4, timeout=0), 1.0)
        request.assert_called_once()

    def test_partial_entry_brackets_only_the_confirmed_filled_quantity(self):
        bracket = {
            "total_qty": 2,
            "entry": 100.0,
            "sl": 101.0,
            "risk": 1.0,
            "q1": 1,
            "sl_id": "sl-1",
            "tp_id": "tp-1",
        }
        with (
            patch.object(main, "ENABLE_LIVE_ORDERS", True),
            patch.object(main, "AUTO_EXECUTE", True),
            patch.object(main, "is_authenticated", return_value=True),
            patch.object(main, "adjust_qty_for_lot_size", return_value=4),
            patch.object(main, "check_margin_sufficient", return_value=True),
            patch.object(main, "send_telegram"),
            patch.object(main, "place_entry", return_value={
                "success": True,
                "result": {"id": "entry-1"},
            }),
            patch.object(main, "wait_fill", return_value=2.0),
            patch.object(main, "place_brackets", return_value=bracket) as brackets,
            patch.object(main, "manage_trailing", return_value={
                "exit_reason": "CLOSED",
                "sl_hit": False,
            }),
        ):
            result = main.execute_trade(
                "123",
                "TESTUSD",
                {
                    "quantity": 4,
                    "entry": 100.0,
                    "stop_loss": 101.0,
                    "take_profit": 98.0,
                    "trade_capital": 10.0,
                },
                100.0,
                1.0,
            )
        self.assertEqual(result["exit_reason"], "CLOSED")
        self.assertEqual(brackets.call_args.args[1], 2.0)

    def test_unprotected_partial_position_requests_emergency_close(self):
        with (
            patch.object(main, "ENABLE_LIVE_ORDERS", True),
            patch.object(main, "AUTO_EXECUTE", True),
            patch.object(main, "is_authenticated", return_value=True),
            patch.object(main, "adjust_qty_for_lot_size", return_value=4),
            patch.object(main, "check_margin_sufficient", return_value=True),
            patch.object(main, "send_telegram"),
            patch.object(main, "place_entry", return_value={
                "success": True,
                "result": {"id": "entry-1"},
            }),
            patch.object(main, "wait_fill", return_value=2.0),
            patch.object(main, "place_brackets", return_value={"sl_id": None}),
            patch.object(main, "market_close", return_value={"success": True}) as close,
            patch.object(main, "manage_trailing") as manage,
        ):
            result = main.execute_trade(
                "123",
                "TESTUSD",
                {
                    "quantity": 4,
                    "entry": 100.0,
                    "stop_loss": 101.0,
                    "take_profit": 98.0,
                    "trade_capital": 10.0,
                },
                100.0,
                1.0,
            )
        self.assertIsNone(result)
        close.assert_called_once_with("123", 2.0)
        manage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
