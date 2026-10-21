
import os
import sys
import json
import time
import uuid
import asyncio
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from loguru import logger
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SRC_ROOT = os.path.join(PROJECT_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

# Load Env (OpenAI Key)
load_dotenv(dotenv_path=os.path.join(PROJECT_ROOT, ".env"))

from core.redpanda_bus import RedpandaEventBus
from core.risk_controller import HardRiskGatekeeper
from core.execution.router import ExecutionRouter
import redis

# ✅ P0 组件（阶段 1：导入新组件）
from core.config import ConfigManager
# from agentic_layer import AgentOrchestrator  # ARCHIVED for V2.1

# ✅ V2.1 LangGraph Components — lazy imports to avoid torch/cv2 at module level
# Heavy chain: HeadTraderNode → rl_service → sb3_contrib → torch (~2GB RAM, ~5s load)
# Light chain: build_graph, DataPrepNode, etc. are safe at module level
from src.agentic_layer.graph import build_graph
from src.agentic_layer.nodes.data_prep import DataPrepNode
from src.agentic_layer.nodes.external_intent_consumer import ExternalIntentConsumer
from src.agentic_layer.nodes.suggestion_consumer import SuggestionConsumerNode
from src.infrastructure.data_service import IBKRDataService, MockDataService
# Lazy imports (inside methods):
#   MarketAnalystNode — pulls langchain_google_genai + langchain_openai
#   HeadTraderNode — pulls rl_service → sb3_contrib → torch
#   rl_service — pulls sb3_contrib → torch
#   ChatOpenAI — only used in _create_llm()

# ✅ P1-2 组件：MarketRegimeProvider
from core.risk_regime_provider import RedisRegimeProvider
from stockapp_v12.core.limits import v12_enabled
from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
from stockapp_v12.pipeline.journal import TradeJournal

class ShadowRunner:
    def __init__(self, bus: RedpandaEventBus, env: str = "dev", mock_mode: bool = False):
        self.bus = bus

        # ✅ 阶段 2：初始化 ConfigManager
        self.config = ConfigManager(env=env)

        # ✅ 阶段 2：使用 ConfigManager 获取活跃模式
        self.active_modes = self.config.get_active_modes()

        # ✅ 自动检测是否为 Shadow 模式
        # 如果所有活跃模式都是 shadow_ 前缀，则启用 mock_mode
        self.is_shadow_mode = all(
            mode.startswith("shadow_") for mode in self.active_modes
        )
        if self.is_shadow_mode:
            logger.info("👻 Shadow Mode detected - skipping Market Analyst (no real news)")
            mock_mode = True

        # ✅ 阶段 2：初始化 Redis（在 orchestrator 之前）
        redis_cfg = self.config.get_redis_config()
        self.redis = redis.Redis(**redis_cfg.__dict__)

        # ✅ V2.1：初始化 DataService
        if mock_mode:
            self.data_service = MockDataService()
        else:
            # Assuming IBKR Gateway info is in config
            ib_cfg = self.config.get_ibkr_config() if hasattr(self.config, 'get_ibkr_config') else None
            host = ib_cfg.host if ib_cfg else "127.0.0.1"
            port = ib_cfg.port if ib_cfg else 4002
            self.data_service = IBKRDataService(host=host, port=port)

        # ✅ V2.1：初始化 LangGraph 智能层
        self.agent_app = self._init_agent_graph(mock_mode)

        # ⚠️ 保留（后续解耦）
        # ✅ 阶段 8：注入 Redis 到 ExecutionRouter
        self.router = ExecutionRouter(
            config_manager=self.config,
            redis_client=self.redis,
            bus=self.bus
        )

        # ✅ 阶段 3：注入 RedisRegimeProvider
        redis_regime_provider = RedisRegimeProvider(self.redis)
        self.hard_risk = HardRiskGatekeeper(
            initial_capital=100000.0,
            regime_provider=redis_regime_provider,
            config_manager=self.config
        )

        # ✅ Phase A: Bounded thread pool (max 4 concurrent crew runs)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="crew")

        # Watchlist Keys (from scanner)
        self.watchlist_ranked_key = "watchlist:ranked"
        self.watchlist_current_key = "watchlist:current"

    # ✅ V2.1：LangGraph 初始化
    def _init_agent_graph(self, mock_mode: bool, checkpointer=None):
        """初始化 LangGraph 智能层"""
        # Lazy imports — only load heavy deps when actually building the graph
        from src.agentic_layer.nodes.market_analyst import MarketAnalystNode
        from src.agentic_layer.nodes.head_trader import HeadTraderNode

        logger.info(f"🕸️ Building LangGraph intelligent layer (mock_mode={mock_mode})")
        
        # 1. Initialize Nodes
        prep_node = DataPrepNode(data_service=self.data_service, redis_url=self.config.get_redis_url())
        suggestion_node = SuggestionConsumerNode(redis_url=self.config.get_redis_url())
        consumer_node = ExternalIntentConsumer()
        
        # Real or Mock Analyst/Trader Nodes
        if mock_mode:
            analyst_node = None # Use default mocks in build_graph
            trader_node = None
        else:
            analyst_node = MarketAnalystNode(data_service=self.data_service, redis_url=self.config.get_redis_url())
            trader_node = HeadTraderNode(data_service=self.data_service)

        # 2. Build and Compile Graph
        return build_graph(
            analyst_node=analyst_node,
            trader_node=trader_node,
            prep_node=prep_node,
            suggestion_node=suggestion_node,
            consumer_node=consumer_node,
            gatekeeper_secret=os.getenv("GATEKEEPER_SECRET"),  # required — build_graph fails fast if missing
            checkpointer=checkpointer
        )

    def _create_llm(self, llm_cfg):
        """创建 LLM Provider（使用 LLMFactory 统一管理）"""
        from langchain_openai import ChatOpenAI
        from core.llm import LLMFactory

        # ✅ 使用 LLMFactory 验证配置，但继续使用 LangChain 的 ChatOpenAI
        # 这样可以保持与现有工具链的兼容性，同时提供统一的配置管理

        if llm_cfg.provider in ["deepseek", "zai"]:
            # OpenAI 兼容的 Provider
            return ChatOpenAI(
                api_key=llm_cfg.api_key,
                base_url=llm_cfg.base_url,
                model=llm_cfg.model,
                temperature=0.7
            )
        elif llm_cfg.provider == "openai":
            return ChatOpenAI(
                api_key=llm_cfg.api_key,
                model=llm_cfg.model,
                temperature=0.7
            )
        elif llm_cfg.provider == "xai":
            # xAI (Grok) - OpenAI 兼容
            return ChatOpenAI(
                api_key=llm_cfg.api_key,
                base_url="https://api.x.ai/v1",
                model=llm_cfg.model,
                temperature=0.7
            )
        else:
            logger.warning(f"⚠️  Unknown LLM provider: {llm_cfg.provider}, using mock")
            return None

    def get_current_watchlist(self, top_n: int = 40) -> list:
        """
        Get current watchlist from Redis (populated by IntradayScanner).
        
        Returns list of symbols ordered by composite score.
        """
        try:
            symbols = self.redis.zrevrange(self.watchlist_ranked_key, 0, top_n - 1)
            if symbols:
                logger.info(f"📋 Watchlist loaded: {len(symbols)} symbols (Top: {symbols[:5]})")
                return list(symbols)
            else:
                logger.warning("⚠️ Watchlist is empty, scanner may not have run yet")
                return []
        except Exception as e:
            logger.error(f"❌ Failed to get watchlist from Redis: {e}")
            return []
    
    def get_watchlist_detail(self, symbol: str) -> dict:
        """Get full details for a watchlist symbol"""
        try:
            data = self.redis.hget(self.watchlist_current_key, symbol)
            if data:
                import json
                return json.loads(data)
            return {}
        except Exception as e:
            logger.error(f"Failed to get watchlist detail for {symbol}: {e}")
            return {} 
    
    def start(self):
        logger.info("🕵️ Shadow Runner Starting (Custom Agent Mode)...")
        self.bus.subscribe("events.pattern", self.on_pattern_detected)
        self.bus.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.bus.stop()
            logger.info("Shadow Runner Stopped.")



    def on_pattern_detected(self, message):
        """
        ✅ 增强版本：支持多种 Kafka 消息格式
        """
        try:
            # 记录原始消息类型
            logger.info(f"📨 [DEBUG] Received message type: {type(message).__name__}")
            
            # 提取 symbol 和 event_data
            symbol = None
            event_data = None

            # 方式 1: Kafka Consumer Message 对象
            if hasattr(message, 'key') and hasattr(message, 'value'):
                symbol = message.key
                value = message.value

                logger.info(f"   [DEBUG] Message.key: {symbol}")
                logger.info(f"   [DEBUG] Message.value type: {type(value).__name__}")

                # 如果 value 已经是 dict
                if isinstance(value, dict):
                    event_data = value
                    logger.info(f"   [DEBUG] Event data (direct dict)")
                # 如果 value 是字符串（需要解析）
                elif isinstance(value, str):
                    try:
                        event_data = json.loads(value)
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"JSON parse failed for string value: {e}")
                        event_data = value
                # 如果 value 是字节串（需要解码）
                elif isinstance(value, bytes):
                    try:
                        decoded = value.decode('utf-8')
                        event_data = json.loads(decoded)
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"JSON parse failed for bytes value: {e}")
                        event_data = value
                else:
                    logger.error(f"   [ERROR] Unsupported value type: {type(value)}")
                    return

            # 方式 2: 直接就是 dict
            elif isinstance(message, dict):
                symbol = message.get('key', message.get('symbol'))
                value = message.get('value', message)

                if symbol and value:
                    if isinstance(value, dict):
                        event_data = value
                    elif isinstance(value, str):
                        try:
                            event_data = json.loads(value)
                        except (json.JSONDecodeError, ValueError):
                            event_data = value
                    else:
                        event_data = value

                    logger.info(f"   [DEBUG] From dict - Symbol: {symbol}")
                    logger.info(f"   [DEBUG] Event data (from dict)")
                else:
                    event_data = message
                    symbol = event_data.get('symbol', 'UNKNOWN')
                    logger.info(f"   [DEBUG] From direct - Symbol: {symbol}")
                    logger.info(f"   [DEBUG] Event data (direct)")

            # 验证必要字段
            if not symbol or not event_data:
                logger.warning("Missing required fields, skipping")
                return

            # 提取 pattern
            pattern_type = event_data.get('pattern', event_data.get('type', 'UNKNOWN'))
            source = event_data.get('source', 'yolo')
            if source == 'forced_inspection':
                logger.info(f"[FORCED INSPECTION] {symbol} — bypassing YOLO pattern wait")
            else:
                logger.info(f"Event Received: {pattern_type} on {symbol}")

            # 运行 Agent Crew (bounded pool — max 4 concurrent crews)
            self._executor.submit(self._run_crew, symbol, event_data)

        except Exception as e:
            logger.error(f"❌ [FATAL] Error in on_pattern_detected: {e}")
            import traceback
            traceback.print_exc()


    def _run_crew(self, symbol: str, event_data: dict):
        """✅ V2.1：使用 LangGraph 运行智能决策流程"""
        logger.info(f"🕸️ Invoking LangGraph for {symbol}...")

        # Create a mock result object for downstream compatibility
        class MockResult:
            def __init__(self, success, verdict, metadata, error=None):
                self.success = success
                self.final_verdict = verdict
                self.metadata = metadata
                self.error = error
                # Attributes referenced by _log_trade but not always set
                self.analyst = None
                self.trader = None
                self.execution_time_sec = None

        # Initialize result with a default failure state
        result = MockResult(success=False, verdict=None, metadata={}, error="Initialization")
        final_state = {}
        audit_log = {}

        try:
            # ✅ 同步调用 LangGraph (维持 Threading 兼容性)
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)

                async def run_async():
                    config = {"configurable": {"thread_id": f"shadow_{symbol}_{int(time.time())}"}}
                    input_state = {
                        "symbol": symbol,
                        "event_data": event_data,
                        "trace_id": event_data.get("trace_id", f"tr_{int(time.time())}"),
                        "external_command": None,
                        "external_suggestions": [],
                        "analyst_signal": None,
                        "current_intention": None,
                        "negative_lesson_warnings": [],
                    }
                    return await self.agent_app.ainvoke(input_state, config=config)

                final_state = loop.run_until_complete(run_async())
            finally:
                loop.close()

            # ✅ Success extraction
            intention = final_state.get("current_intention")
            graph_error = final_state.get("error")
            success = intention is not None and not graph_error
            trace_id = final_state.get("trace_id", "UNKNOWN_TRACE")
            
            result = MockResult(
                success=success,
                verdict=intention,
                metadata=final_state.get("fusion_metadata", {}),
                error=final_state.get("error")
            )
            
            # Pass audit_log downstream
            audit_log = final_state.get("audit_log", {})

            # ✅ SUCCESS path: graph ran without exception
            if result.success:
                # RL V18 prediction (lazy import — avoids loading torch at module level)
                try:
                    from services.rl_service import rl_service
                    rl_result = rl_service.predict(None, symbol)  # TODO: pass actual 5m DataFrame
                    if "target_position" in rl_result:
                        logger.info(f"🤖 [RL V18] Prediction for {symbol}: target_pos={rl_result['target_position']}")
                        result.metadata['rl_v18'] = rl_result
                except Exception as re:
                    logger.error(f"⚠️ [RL V18] Prediction failed: {re}")

                # Extract intent side from verdict
                final_verdict = result.final_verdict
                if hasattr(final_verdict, 'side'):
                    intent_side = final_verdict.side
                elif isinstance(final_verdict, dict):
                    intent_side = final_verdict.get("side", "HOLD")
                else:
                    intent_side = "HOLD"

                logger.info(f"🎯 Final Verdict: {intent_side}")
                self._fanout_execution(symbol, intent_side, event_data, result, trace_id, audit_log)
            else:
                logger.error(f"❌ Orchestrator failed: {result.error}")

        except Exception as e:
            logger.error(f"❌ _run_crew failed: {e}")
            result.error = str(e)
            logger.error(f"❌ Orchestrator failed: {result.error}")

    def _fanout_execution(self, symbol, intent_side, event_data, orchestrator_result, trace_id, audit_log):
        """✅ 阶段 5：Fan-out 执行到所有活跃模式"""
        for mode in self.active_modes:
            self._execute_for_mode(symbol, intent_side, event_data, mode, orchestrator_result, trace_id, audit_log)

    def _execute_for_mode(self, symbol, intent_side, event_data, mode, orchestrator_result, trace_id, audit_log):
        """✅ 阶段 5：执行单个模式"""
        logger.info(f"🔄 Processing Mode: {mode}")

        # A. Get Portfolio State for Mode
        port_key = f"portfolio:snapshot:{mode}"
        port_data = self.redis.get(port_key)
        if port_data:
            port_state = json.loads(port_data)
            # Update Gatekeeper State
            self.hard_risk.update_account_state(
                equity=port_state.get('nav', 100000.0),
                pnl=port_state.get('daily_pnl', 0.0),  # Ensure broker provides this
                positions=port_state.get('positions', {})
            )
        else:
            logger.warning(f"   ⚠️ No Portfolio State for {mode}, using default Risk State")

        # B. Risk Check (Mode specific)
        gatekeeper_result = "SKIPPED"
        risk_meta = {}
        verdict_text = str(orchestrator_result.final_verdict)

        was_lesson_applied = audit_log.get("was_lesson_applied", False)
        
        if intent_side in ['BUY', 'SELL'] or (intent_side == 'HOLD' and was_lesson_applied):
            # Get symbol metrics from watchlist cache for regime tier classification
            symbol_metrics = self.redis.hget("watchlist:current", symbol)
            atr_pct = 0.05
            beta = 1.6
            if symbol_metrics:
                try:
                    m = json.loads(symbol_metrics)
                    atr_pct = m.get('atr_percent', 0.05)
                    beta = m.get('beta', 1.6)
                except:
                    pass

            approved = True
            reason = "Automatic approval for HOLD/Shadow"
            risk_meta = {}

            if intent_side in ['BUY', 'SELL']:
                journal = TradeJournal(self.redis) if v12_enabled() else None
                entry_guard = (
                    EntryEvaluationGuard(self.redis, mode=mode)
                    if v12_enabled()
                    else None
                )

                if intent_side == 'BUY' and entry_guard is not None:
                    ok, skip_code = entry_guard.begin_entry_evaluation(symbol, mode=mode)
                    if not ok:
                        logger.info(f"   ⏭️ [{mode}] Entry skipped: {skip_code}")
                        if journal:
                            journal.log_pass(
                                symbol, mode, skip_code, f"Entry evaluation skipped: {skip_code}"
                            )
                        gatekeeper_result = skip_code
                        self._log_trade(symbol, orchestrator_result, gatekeeper_result, risk_meta, event_data, mode)
                        return

                # Extract confidence from orchestrator result (analyst signal or fallback)
                agent_confidence = 0.5
                try:
                    fusion_meta = orchestrator_result.metadata or {}
                    if isinstance(fusion_meta, dict):
                        analyst_signal = fusion_meta.get('analyst_signal', {})
                        if isinstance(analyst_signal, dict):
                            agent_confidence = float(analyst_signal.get('confidence', 0.5))
                        else:
                            agent_confidence = float(getattr(analyst_signal, 'confidence', 0.5))
                except (ValueError, TypeError, AttributeError):
                    pass

                # P0: Use live price from Redis, not stale scan-time price
                price = self._get_live_price(symbol, event_data.get('price'))
                if price is None or price <= 0:
                    logger.warning(f"   ⚠️ [{mode}] No valid live price for {symbol}, skipping execution")
                    gatekeeper_result = "SKIPPED_STALE_PRICE"
                    self._log_trade(symbol, orchestrator_result, gatekeeper_result, risk_meta, event_data, mode)
                    return

                atr_14 = None
                if symbol_metrics:
                    try:
                        m = json.loads(symbol_metrics)
                        atr_14 = m.get("atr_14") or m.get("atr")
                    except Exception:
                        pass

                approved, reason, risk_meta = self.hard_risk.validate_order_intent(
                    symbol=symbol,
                    side=intent_side,
                    confidence=agent_confidence,
                    price=price,
                    atr_pct=atr_pct,
                    beta=beta,
                    atr_14=atr_14,
                )

                if journal and intent_side == 'BUY':
                    if approved:
                        journal.log_allow(symbol, mode, risk_meta)
                    else:
                        reason_code = reason.split(":")[0] if ":" in reason else "DENY"
                        journal.log_deny(
                            symbol,
                            mode,
                            reason_code,
                            reason,
                            requested_size_pct=risk_meta.get("size_pct", 0.0),
                        )

            if approved:
                logger.success(f"   ✅ [{mode}] Gatekeeper APPROVED: {intent_side} {symbol}")
                gatekeeper_result = "APPROVED"

                # C. Route Order
                try:
                    # Use details from Risk Meta (Sizing/SL/TP)
                    size_pct = risk_meta.get('size_pct', 0.01)
                    equity = self.hard_risk.current_equity
                    # price already resolved to live price above
                    qty = int((equity * size_pct) / price) if price > 0 else 0

                    # D. Generate Intent Context for Auditability
                    intent_id = str(uuid.uuid4())
                    regime = "UNKNOWN"
                    try:
                        regime = self.hard_risk._get_current_regime()
                    except:
                        pass
                        
                    intent_context = {
                        "intent_id": intent_id,
                        "symbol": symbol,
                        "side": intent_side,
                        "pattern_type": event_data.get('pattern', 'UNKNOWN'),
                        "fused_score": getattr(orchestrator_result.final_verdict, 'confidence', 0.0),
                        "regime": regime,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "trace_id": trace_id,
                        "audit_log": audit_log
                    }

                    if qty > 0:
                        res = self.router.route_order(
                            mode=mode,
                            symbol=symbol,
                            action=intent_side,
                            quantity=qty,
                            order_type='MKT',
                            price=price,
                            agent_metadata={
                                "verdict": verdict_text,
                                "risk_meta": risk_meta,
                                "intent_context": intent_context,
                                # V2.2: dominant_source flows to trade_events.risk_metadata for PostMortem
                                "dominant_source": getattr(orchestrator_result, 'metadata', {}).get('dominant_source', 'LLM'),
                            }
                        )
                        logger.info(f"   🚀 [{mode}] Execution Result: {res.get('status')}")
                        if (
                            v12_enabled()
                            and intent_side == 'BUY'
                            and res.get('status') in ('SUBMITTED', 'FILLED', 'SUCCESS', 'simulated')
                        ):
                            self.hard_risk.increment_daily_trade_count()
                    else:
                        logger.warning(f"   ⚠️ [{mode}] Qty 0 calculated")

                except Exception as e:
                    logger.error(f"   ❌ [{mode}] Routing Failed: {e}")
            else:
                logger.warning(f"   🛑 [{mode}] Gatekeeper REJECTED: {reason}")
                gatekeeper_result = "REJECTED"

        # Log Decision per Mode
        self._log_trade(symbol, orchestrator_result, gatekeeper_result, risk_meta, event_data, mode)

    def _get_live_price(self, symbol: str, fallback_price=None) -> float | None:
        """P0: Fetch live price from Redis with 5-min freshness check.
        Returns None if no valid live price is available.
        """
        try:
            live_raw = self.redis.get(f"price:{symbol}:current")
            if live_raw:
                live_data = json.loads(live_raw)
                price = float(live_data.get("price", 0))
                ts = float(live_data.get("timestamp", 0))
                age_sec = time.time() - ts
                if price > 0 and age_sec < 300:
                    logger.debug(f"   💹 Live price for {symbol}: ${price:.2f} (age={age_sec:.0f}s)")
                    return price
                else:
                    logger.warning(
                        f"⚠️ Live price stale for {symbol}: age={age_sec:.0f}s, "
                        f"price=${price:.2f}"
                    )
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch live price for {symbol}: {e}")

        # Fallback to scan-time price only if event carries a recent timestamp
        if fallback_price and fallback_price > 0:
            event_ts = event_data.get("timestamp") if event_data else None
            if event_ts:
                try:
                    event_age = time.time() - float(event_ts)
                    if event_age < 60:
                        logger.warning(f"   Using scan-time price for {symbol}: ${fallback_price:.2f} (event age={event_age:.0f}s)")
                        return float(fallback_price)
                    else:
                        logger.warning(f"   Scan-time price also stale for {symbol}: event age={event_age:.0f}s > 60s")
                except (ValueError, TypeError):
                    # No valid timestamp on event — refuse to use unvalidated price
                    logger.warning(f"   Scan-time price for {symbol}: no event timestamp, refusing")
            else:
                logger.warning(f"   Scan-time price for {symbol}: no event timestamp, refusing")

        return None

    def _log_trade(self, symbol, orchestrator_result, gatekeeper_status, risk_meta, event, mode="default"):
        """✅ 阶段 5：记录交易决策（使用 OrchestratorResult）"""
        entry = {
            "mode": mode,
            "timestamp": time.time(),
            "symbol": symbol,
            "trigger_event": event,
            "agent_verdict": str(orchestrator_result.final_verdict),
            "gatekeeper_status": gatekeeper_status,
            "risk_metadata": risk_meta,
            "orchestrator_meta": {
                "analyst": orchestrator_result.analyst,
                "trader": orchestrator_result.trader,
                "# risk_output": "风控由 HardRiskGatekeeper 负责",
                "execution_time_sec": orchestrator_result.execution_time_sec,
            }
        }

        # 1. Publish to Bus (for Backtest/Staging Consumption)
        try:
            self.bus.publish("events.shadow_decision", entry)
        except Exception as e:
            logger.error(f"Failed to publish shadow decision: {e}")

        # 2. Log to File
        try:
            logs_dir = os.path.join(PROJECT_ROOT, "logs")
            os.makedirs(logs_dir, exist_ok=True)
            log_file = os.path.join(logs_dir, "shadow_trades.jsonl")
            with open(log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Logging failed: {e}")

if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY") or "placeholder" in os.getenv("OPENAI_API_KEY"):
        logger.warning("⚠️ OPENAI_API_KEY is missing/placeholder. Agents will fail to think, but flow will run.")
    
    # 从环境变量读取 Kafka 配置
    kafka_bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

    bus = RedpandaEventBus(bootstrap_servers=kafka_bootstrap, group_id="shadow_runner_custom")
    bus.connect()
    
    runner = ShadowRunner(bus)
    runner.start()
