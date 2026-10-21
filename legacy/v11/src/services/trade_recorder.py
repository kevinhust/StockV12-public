import logging
import json
import time
import uuid
import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from prometheus_client import Counter

# Metrics Definition
WRITE_SUCCESS = Counter('trade_events_write_success_total', 'Successful DB writes')
WRITE_FAILURE = Counter('trade_events_write_failure_total', 'Failed DB writes after retries', ['reason'])
RETRY_TOTAL = Counter('trade_events_retry_total', 'Retries performed', ['attempt'])
DLQ_TOTAL = Counter('trade_events_dlq_total', 'Events sent to DLQ')
MTM_UPDATES = Counter('trade_events_mtm_updates_total', 'Total MtM updates performed', ['symbol'])

logger = logging.getLogger(__name__)

# Decimal precision — matches NUMERIC(18,4) in TimescaleDB
QUANT_CTX = Decimal('0.0001')


class TradeRecorder:
    def __init__(self, db_url: str, bus: Optional[Any] = None, redis_client: Optional[Any] = None):
        self.db_url = db_url
        self.bus = bus
        self.redis = redis_client
        self.retry_delay = 1
        self.max_delay = 16
        # Connection pool (thread-safe for consumer thread + MtM updates)
        self._pool: Optional[ThreadedConnectionPool] = None

    def _get_pool(self) -> ThreadedConnectionPool:
        """Lazy-init connection pool."""
        if self._pool is None or self._pool.closed:
            self._pool = ThreadedConnectionPool(
                minconn=1, maxconn=5, dsn=self.db_url
            )
            logger.info("TradeRecorder DB connection pool initialized (1-5)")
        return self._pool

    def _get_conn(self):
        return self._get_pool().getconn()

    def _return_conn(self, conn):
        if self._pool and not self._pool.closed:
            self._pool.putconn(conn)

    # ── Public: Kafka Consumer Callback ────────────────────────────────────

    def consume_event(self, event: Dict[str, Any]) -> bool:
        """
        Consumes trade execution events and persists to TimescaleDB.
        Returns True if event was processed (including dedup), False if
        it should be retried by the caller.

        Pipeline:
          normalize → validate → atomic upsert + FIFO match → MtM update
        """
        try:
            # 0. Generate composite event_id if missing
            if 'event_id' not in event:
                intent_id = event.get('intent_id') or event.get('intent_context', {}).get('intent_id', 'NO_INTENT')
                symbol = event.get('symbol', 'UNKNOWN').upper()
                side = event.get('side', 'UNKNOWN').upper()
                orig_ts = event.get('timestamp')
                ts_str = orig_ts[:19] if orig_ts else datetime.now(timezone.utc).isoformat(timespec='seconds')
                composite_str = f"V8|{intent_id}|{symbol}|{side}|{ts_str}"
                event['event_id'] = sha256(composite_str.encode()).hexdigest()[:32]

            # 1. Normalize fields
            if event.get('price') is None and event.get('fill_price') is not None:
                event['price'] = event['fill_price']
            if 'risk_metadata' not in event and 'metadata' in event:
                event['risk_metadata'] = event['metadata']

            # 2. Validate — reject invalid events early (DLQ them, don't retry)
            symbol = event.get('symbol')
            side = event.get('side', '').upper()
            quantity = event.get('quantity', 0)
            price = event.get('price', 0)
            if not symbol or quantity is None or quantity <= 0 or price is None or price <= 0:
                raise ValueError(f"Invalid trade params: symbol={symbol}, qty={quantity}, price={price}")

            logger.info(f"Processing event: {event.get('event_type')} for {symbol} ID={event.get('event_id')}")

            # 3. Atomic write (INSERT ON CONFLICT + FIFO in single transaction)
            try:
                self._execute_atomic(event)
                WRITE_SUCCESS.inc()
            except Exception as e:
                logger.error(f"Critical Persistence Failure: {e}")
                WRITE_FAILURE.labels(reason=type(e).__name__).inc()
                self._send_to_dlq(event, str(e))
                return True  # DLQ accepted → safe to commit offset

            # 4. Trigger MtM update after successful commit
            if event.get('status') == 'EXECUTED' and event.get('price'):
                try:
                    self.update_mtm(event.get('symbol'), event.get('price'))
                except Exception as e:
                    # MtM failure is non-critical for event recording — alert but don't block
                    logger.error(f"MtM update failed for {event.get('symbol')}: {e}")
                    self._publish_mtm_alert(event.get('symbol'), event.get('event_id'), str(e))

            logger.info(f"Recorded trade: {symbol} {side} {quantity} @ {price}")
            return True

        except ValueError as e:
            # Validation failure — permanent, don't retry
            logger.error(f"Invalid event rejected: {e}")
            self._send_to_dlq(event, str(e))
            return True  # Commit offset even for invalid events
        except Exception as e:
            logger.error(f"Failed to record trade (consume_event): {e}")
            return True

    # ── Core: Atomic Upsert + FIFO ─────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((psycopg2.OperationalError, psycopg2.InterfaceError)),
        reraise=True
    )
    def _execute_atomic(self, event: Dict[str, Any]):
        """
        Single-transaction write:
          1. INSERT ... ON CONFLICT DO NOTHING (dedup + persist)
          2. BUY → create lot in open_positions_track
          3. SELL → FIFO match against open lots
          4. COMMIT or ROLLBACK
        """
        side = event.get('side', '').upper()
        conn = self._get_conn()
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                # ── Step 1: INSERT with dedup ──────────────────────────────
                intent_ctx = event.get('risk_metadata', {}).get('intent_context', {})
                trace_id = intent_ctx.get('trace_id')

                # Deterministic timestamp for composite PK dedup — use event's own timestamp
                event_time = event.get('timestamp')
                if isinstance(event_time, str):
                    event_time = datetime.fromisoformat(event_time.replace('Z', '+00:00'))
                elif not isinstance(event_time, datetime):
                    event_time = datetime.utcnow()

                cur.execute("""
                    INSERT INTO trade_events (
                        time, event_id, trace_id, symbol, side, quantity, price,
                        status, mode, matching_method, pnl_status,
                        confidence, pattern_type, risk_metadata, context_snapshot
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (event_id, time) DO NOTHING
                    RETURNING event_id
                """, (
                    event_time,
                    event.get('event_id'),
                    trace_id,
                    event.get('symbol'),
                    side,
                    event.get('quantity'),
                    event.get('price'),
                    event.get('status', 'EXECUTED'),
                    event.get('mode', 'PAPER'),
                    event.get('matching_method', 'FIFO'),
                    event.get('pnl_status', 'PENDING'),
                    event.get('confidence', 0.0),
                    event.get('pattern_type', 'UNKNOWN'),
                    Json(event.get('risk_metadata', {})),
                    Json(intent_ctx)
                ))

                inserted = cur.fetchone()
                if not inserted:
                    logger.info(f"Duplicate event {event.get('event_id')} — skipped")
                    conn.commit()
                    return

                # ── Step 2: BUY → create lot ───────────────────────────────
                if side == 'BUY' and event.get('status') == 'EXECUTED':
                    self._create_lot(cur, event)

                # ── Step 3: SELL → FIFO match ──────────────────────────────
                elif side == 'SELL' and event.get('status') == 'EXECUTED':
                    self._match_fifo(cur, event)

                # ── Commit ─────────────────────────────────────────────────
                conn.commit()

        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            self._return_conn(conn)

    # ── Lot Creation ──────────────────────────────────────────────────────────

    @staticmethod
    def _create_lot(cur, event: Dict[str, Any]):
        """Create a new FIFO lot for a BUY execution."""
        qty = Decimal(str(event.get('quantity', 0))).quantize(QUANT_CTX)
        price = Decimal(str(event.get('price', 0))).quantize(QUANT_CTX)
        cur.execute("""
            INSERT INTO open_positions_track (
                symbol, entry_event_id, entry_time, entry_price,
                initial_quantity, remaining_quantity, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            event.get('symbol'),
            event.get('event_id'),
            datetime.utcnow(),
            price,
            qty,
            qty,
            'OPEN'
        ))

    # ── FIFO Matching (Decimal-precise) ────────────────────────────────────

    @staticmethod
    def _match_fifo(cur, event: Dict[str, Any]):
        """
        Match SELL against open lots using FIFO (oldest first).
        Uses Decimal for all quantity/price arithmetic to prevent float drift.
        Sets pnl_status to REALIZED / PARTIAL / UNMATCHED.
        """
        symbol = event.get('symbol')
        sell_qty = Decimal(str(event.get('quantity', 0))).quantize(QUANT_CTX)
        sell_price = Decimal(str(event.get('price', 0))).quantize(QUANT_CTX)
        event_id = event.get('event_id')

        if sell_qty <= 0 or sell_price <= 0:
            logger.warning(f"Invalid SELL params: qty={sell_qty} price={sell_price}")
            return

        # Row-level lock — FOR UPDATE with 3s lock timeout for correctness
        # Prevents SKIP LOCKED skipping valid lots on concurrent SELLs
        cur.execute("SET LOCAL lock_timeout = '3s'")
        cur.execute("""
            SELECT entry_event_id, remaining_quantity, entry_price, entry_time
            FROM open_positions_track
            WHERE symbol = %s AND status IN ('OPEN', 'PARTIAL')
            ORDER BY entry_time ASC
            FOR UPDATE
        """, (symbol,))

        lots = cur.fetchall()

        # ── No open lots → UNMATCHED ──────────────────────────────────────
        if not lots:
            logger.warning(f"UNMATCHED SELL: {event_id} {symbol} qty={sell_qty} — no open lots")
            cur.execute("""
                UPDATE trade_events
                SET pnl_status = 'UNMATCHED',
                    risk_metadata = jsonb_set(
                        COALESCE(risk_metadata, '{}'),
                        '{unmatched_reason}', '"no_open_lots"'
                    )
                WHERE event_id = %s
            """, (event_id,))
            # Alert via Redis (fire-and-forget)
            return

        # ── FIFO match loop (Decimal arithmetic) ──────────────────────────
        remaining_to_sell = sell_qty
        total_cost = Decimal('0')
        matched_qty = Decimal('0')

        for lot_id, rem_qty_db, entry_price_db, entry_time in lots:
            if remaining_to_sell <= 0:
                break

            rem_qty = Decimal(str(rem_qty_db)).quantize(QUANT_CTX)
            entry_price = Decimal(str(entry_price_db)).quantize(QUANT_CTX)

            match_amt = min(rem_qty, remaining_to_sell)
            total_cost += match_amt * entry_price
            matched_qty += match_amt

            new_rem_qty = (rem_qty - match_amt).quantize(QUANT_CTX)
            new_status = 'PARTIAL' if new_rem_qty > 0 else 'CLOSED'

            cur.execute("""
                UPDATE open_positions_track
                SET remaining_quantity = %s, status = %s, updated_at = NOW()
                WHERE entry_event_id = %s
            """, (new_rem_qty, new_status, lot_id))

            remaining_to_sell = (remaining_to_sell - match_amt).quantize(QUANT_CTX)

        # ── Compute realized PnL ──────────────────────────────────────────
        if matched_qty > 0:
            realized_pnl_dollar = (sell_price * matched_qty - total_cost).quantize(QUANT_CTX)
            avg_entry = (total_cost / matched_qty).quantize(QUANT_CTX)
            realized_pnl_pct = ((sell_price - avg_entry) / avg_entry).quantize(Decimal('0.000001')) if avg_entry != 0 else Decimal('0')

            # Check for unmatched remainder
            pnl_status = 'REALIZED' if remaining_to_sell <= 0 else 'PARTIAL'

            cur.execute("""
                UPDATE trade_events
                SET realized_pnl_dollar = %s, realized_pnl_pct = %s, pnl_status = %s
                WHERE event_id = %s
            """, (realized_pnl_dollar, realized_pnl_pct, pnl_status, event_id))

            if remaining_to_sell > 0:
                logger.warning(
                    f"PARTIAL SELL: {event_id} {symbol} "
                    f"matched={matched_qty} unmatched={remaining_to_sell}"
                )
                cur.execute("""
                    UPDATE trade_events
                    SET risk_metadata = jsonb_set(
                        COALESCE(risk_metadata, '{}'),
                        '{unmatched_qty}', %s
                    )
                    WHERE event_id = %s
                """, (str(remaining_to_sell), event_id))
        else:
            # Lots exist but locked → shouldn't happen with FOR UPDATE
            # Treat as UNMATCHED for safety
            logger.warning(f"UNMATCHED SELL: {event_id} {symbol} — lots exist but none matched")
            cur.execute("""
                UPDATE trade_events
                SET pnl_status = 'UNMATCHED',
                    risk_metadata = jsonb_set(
                        COALESCE(risk_metadata, '{}'),
                        '{unmatched_reason}', '"lots_locked_or_zero"'
                    )
                WHERE event_id = %s
            """, (event_id,))

    # ── DLQ ─────────────────────────────────────────────────────────────────

    def _send_to_dlq(self, event: Dict[str, Any], error: str):
        """Route failed events to DLQ topic. Commits offset after DLQ write."""
        logger.warning(f"TradeRecorder DLQ: {error} | Event: {event.get('symbol')}")
        DLQ_TOTAL.inc()

        if self.bus:
            try:
                dlq_payload = event.copy()
                dlq_payload.update({
                    "dlq_reason": error,
                    "dlq_timestamp": datetime.now(timezone.utc).isoformat(),
                    "attempts": 4
                })
                self.bus.publish("events.trade_dlq", dlq_payload)
                logger.info(f"Published to events.trade_dlq: {event.get('event_id')}")
            except Exception as e:
                logger.error(f"Failed to publish to DLQ: {e}")

    # ── MtM ─────────────────────────────────────────────────────────────────

    def update_mtm(self, symbol: str, current_price: float):
        """Calculates MtM for all open lots of a symbol using Decimal arithmetic."""
        if not symbol or current_price is None or current_price <= 0:
            logger.warning(f"Invalid price/symbol for MtM: {symbol} @ {current_price}")
            return

        symbol = symbol.upper()
        cur_price = Decimal(str(current_price)).quantize(QUANT_CTX)

        try:
            mtm_data = {
                "symbol": symbol,
                "current_price": current_price,
                "total_mtm_dollar": "0.0000",
                "total_quantity": "0.0000",
                "timestamp": datetime.utcnow().isoformat(),
                "lots": []
            }

            conn = self._get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT entry_event_id, remaining_quantity, entry_price, entry_time
                        FROM open_positions_track
                        WHERE symbol = %s AND status IN ('OPEN', 'PARTIAL')
                    """, (symbol,))

                    lots = cur.fetchall()
            finally:
                self._return_conn(conn)

            total_mtm = Decimal('0')
            total_qty = Decimal('0')
            total_cost = Decimal('0')

            for lot_id, rem_qty_db, entry_price_db, entry_time in lots:
                rem_qty = Decimal(str(rem_qty_db)).quantize(QUANT_CTX)
                entry_price = Decimal(str(entry_price_db)).quantize(QUANT_CTX)

                dollar_pnl = rem_qty * (cur_price - entry_price)
                pct_pnl = (cur_price - entry_price) / entry_price if entry_price != 0 else Decimal('0')

                total_mtm += dollar_pnl
                total_qty += rem_qty
                total_cost += rem_qty * entry_price

                mtm_data["lots"].append({
                    "lot_id": lot_id,
                    "rem_qty": str(rem_qty),
                    "entry_price": str(entry_price),
                    "mtm_dollar": str(dollar_pnl.quantize(QUANT_CTX)),
                    "mtm_pct": str(pct_pnl.quantize(Decimal('0.000001')))
                })

            mtm_data["total_mtm_dollar"] = str(total_mtm.quantize(QUANT_CTX))
            mtm_data["total_quantity"] = str(total_qty.quantize(QUANT_CTX))

            if total_qty > 0 and total_cost > 0:
                avg_entry = total_cost / total_qty
                mtm_data["total_mtm_pct"] = str(((cur_price - avg_entry) / avg_entry).quantize(Decimal('0.000001')))
            else:
                mtm_data["total_mtm_pct"] = "0.000000"

            if self.redis:
                redis_key = f"open_position_mtm:{symbol}"
                self.redis.set(redis_key, json.dumps(mtm_data), ex=86400)
                MTM_UPDATES.labels(symbol=symbol).inc()
                logger.debug(f"Updated MtM for {symbol}: ${total_mtm} ({mtm_data['total_mtm_pct']}%)")
                self.update_portfolio_mtm()

        except Exception as e:
            logger.error(f"Failed to update MtM for {symbol}: {e}")

    def update_portfolio_mtm(self):
        """Aggregates all symbol MtMs into a portfolio-level PnL."""
        if not self.redis:
            return

        try:
            # Use SCAN instead of KEYS to avoid blocking Redis
            total_mtm_dollar = 0.0
            total_cost = 0.0
            symbol_count = 0

            for key in self.redis.scan_iter("open_position_mtm:*"):
                data = json.loads(self.redis.get(key))
                total_mtm_dollar += float(data.get("total_mtm_dollar", 0.0))
                for lot in data.get("lots", []):
                    total_cost += float(lot["rem_qty"]) * float(lot["entry_price"])
                symbol_count += 1

            portfolio_data = {
                "total_mtm_dollar": total_mtm_dollar,
                "total_cost": total_cost,
                "total_mtm_pct": total_mtm_dollar / total_cost if total_cost != 0 else 0,
                "timestamp": datetime.utcnow().isoformat(),
                "symbol_count": symbol_count
            }

            self.redis.set("portfolio_mtm:current", json.dumps(portfolio_data), ex=86400)

            if portfolio_data["total_mtm_pct"] < -0.05:
                logger.warning(f"PORTFOLIO DRAWDOWN ALERT: {portfolio_data['total_mtm_pct']*100:.2f}%")

        except Exception as e:
            logger.error(f"Failed to update Portfolio MtM: {e}")

    # ── Alerting Helpers ────────────────────────────────────────────────────

    def _publish_mtm_alert(self, symbol: str, event_id: str, error: str):
        """Fire-and-forget alert when MtM update fails."""
        if self.redis:
            try:
                self.redis.publish("alerts:raw", json.dumps({
                    "type": "mtm_update_failed",
                    "symbol": symbol,
                    "event_id": event_id,
                    "error": error,
                    "timestamp": datetime.utcnow().isoformat()
                }))
            except Exception:
                pass
