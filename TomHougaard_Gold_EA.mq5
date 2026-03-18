//+------------------------------------------------------------------+
//|  TomHougaard_Gold_EA.mq5                                        |
//|  Tom Hougaard Price Action Strategy for XAUUSD (Gold)           |
//|                                                                  |
//|  Strategies:                                                     |
//|    1. Opening Range Breakout  (15-min OR, 90-min window)        |
//|    2. Mind the Gap            (fade session-open gaps)          |
//|    3. 62 EMA Trend Filter     (direction alignment)             |
//|    4. Scenario Swings         (weekly high pattern)             |
//|                                                                  |
//|  Risk Rules:                                                     |
//|    - 1% equity risk per trade                                   |
//|    - Stop beyond swing high/low                                 |
//|    - Trailing stop at 1.5 R                                     |
//|    - Max 2 trades per session                                    |
//|    - 2% daily loss limit                                        |
//+------------------------------------------------------------------+
#property copyright   "Tom Hougaard Strategy EA"
#property link        ""
#property version     "1.00"
#property description "Tom Hougaard Price Action for XAUUSD"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//+------------------------------------------------------------------+
//|  Input Parameters                                                |
//+------------------------------------------------------------------+

//--- General
input ulong    InpMagic          = 20240101;     // Magic Number
input string   InpComment        = "TomH_Gold";  // Order Comment
input bool     InpPaperMode      = true;          // Paper Mode (log only, no orders)

//--- Session Times (UTC)
input int      InpLondonHour     = 8;    // London Open Hour  (UTC)
input int      InpLondonMin      = 0;    // London Open Minute
input int      InpNYHour         = 13;   // NY Open Hour      (UTC)
input int      InpNYMin          = 30;   // NY Open Minute

//--- Opening Range Breakout
input bool     InpBreakoutOn     = true;   // Enable Breakout Strategy
input int      InpORMinutes      = 15;     // Opening Range Duration (min)
input int      InpSessionWindow  = 90;     // Max Trade Window  (min from open)
input double   InpMaxCandleMult  = 2.0;    // Skip if candle > N x average size
input int      InpAvgLookback    = 20;     // Lookback for average candle size

//--- Mind the Gap
input bool     InpGapOn          = true;   // Enable Gap Strategy
input double   InpMinGapPts      = 20.0;   // Minimum Gap  (points)
input double   InpMaxGapPts      = 200.0;  // Maximum Gap  (points)

//--- 62 EMA Trend Filter
input bool     InpEMAOn          = true;   // Enable EMA Filter
input int      InpEMAPeriod      = 62;     // EMA Period
input bool     InpEMASlope       = true;   // Require EMA Slope in Trade Direction

//--- Scenario-Based Swings
input bool     InpScenarioOn     = true;   // Enable Scenario Strategy
input bool     InpScenarioA      = true;   // Scenario A: Mon>Tue & Wed -> SHORT Thu
input bool     InpScenarioB      = true;   // Scenario B: Thu>Fri       -> SHORT Fri

//--- Risk Management
input double   InpRiskPct        = 1.0;    // Risk per Trade (% of equity)
input double   InpDailyLossLimit = 2.0;    // Daily Loss Limit (% of equity)
input int      InpMaxTrades      = 2;      // Max Trades per Session
input double   InpTrailR         = 1.5;    // Trailing Stop Trigger (R-multiple)
input double   InpStopBufPts     = 3.0;    // Extra buffer beyond swing (points)
input int      InpSwingLookback  = 10;     // Candles to scan for swing high/low

//+------------------------------------------------------------------+
//|  Global State                                                    |
//+------------------------------------------------------------------+
CTrade         g_trade;

int            g_ema_handle = INVALID_HANDLE;
double         g_ema_buf[];

//--- Opening range state
double         g_or_high    = 0.0;
double         g_or_low     = 0.0;
bool           g_or_formed  = false;
bool           g_or_fired   = false;

//--- Gap state
bool           g_gap_checked   = false;
double         g_prev_close    = 0.0;

//--- Session / daily counters
int            g_trades_today  = 0;
double         g_day_start_eq  = 0.0;
datetime       g_last_reset    = 0;
datetime       g_last_bar      = 0;

//--- Active trade for trailing stop management
double         g_entry         = 0.0;
double         g_stop          = 0.0;
double         g_r_dist        = 0.0;
int            g_dir           = 0;      // +1 = long,  -1 = short
bool           g_trailing_on   = false;

//--- Scenario weekly data  (index = MT5 day_of_week: 1=Mon .. 5=Fri)
double         g_w_high[7];
double         g_w_low[7];
double         g_w_close[7];
bool           g_sc_a_fired    = false;
bool           g_sc_b_fired    = false;

//+------------------------------------------------------------------+
//|  OnInit                                                          |
//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(10);
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);

   if(InpEMAOn)
   {
      g_ema_handle = iMA(_Symbol, PERIOD_CURRENT, InpEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
      if(g_ema_handle == INVALID_HANDLE)
      {
         Print("ERROR: Could not create EMA handle.");
         return INIT_FAILED;
      }
   }

   ArraySetAsSeries(g_ema_buf, true);
   ArrayInitialize(g_w_high,  0.0);
   ArrayInitialize(g_w_low,   0.0);
   ArrayInitialize(g_w_close, 0.0);

   g_day_start_eq = AccountInfoDouble(ACCOUNT_EQUITY);
   g_last_reset   = TimeCurrent();

   PrintFormat("TomHougaard Gold EA started | %s | Magic: %I64u | Paper: %s",
               _Symbol, InpMagic, InpPaperMode ? "YES" : "NO");
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//|  OnDeinit                                                        |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_ema_handle != INVALID_HANDLE)
      IndicatorRelease(g_ema_handle);
}

//+------------------------------------------------------------------+
//|  OnTick                                                          |
//+------------------------------------------------------------------+
void OnTick()
{
   datetime bar_time = iTime(_Symbol, PERIOD_CURRENT, 0);

   //--- Manage trailing stop on every tick (not just new bar)
   if(HavePosition())
      ManageTrailingStop();

   //--- Remaining logic runs once per new closed bar
   if(bar_time == g_last_bar)
      return;
   g_last_bar = bar_time;

   //--- Daily reset
   CheckDailyReset();

   //--- Update weekly data for scenario strategy
   if(InpScenarioOn)
      RefreshWeeklyData();

   //--- If already in a trade, skip entry logic
   if(HavePosition())
      return;

   //--- Hard gates
   if(DailyLossLimitHit())
   {
      PrintFormat("[GATE] Daily loss limit reached — no new entries.");
      return;
   }
   if(g_trades_today >= InpMaxTrades)
      return;

   //--- Current time info
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);

   //--- Strategy 1: Mind the Gap (fires once near session open)
   if(InpGapOn && !g_gap_checked)
      RunGapStrategy(dt);

   if(HavePosition()) return;

   //--- Strategy 2: Opening Range Breakout
   if(InpBreakoutOn)
      RunBreakoutStrategy(dt);

   if(HavePosition()) return;

   //--- Strategy 3: Scenario swings (only at daily close)
   if(InpScenarioOn && IsNearDailyClose(dt))
      RunScenarioStrategy(dt);
}

//+------------------------------------------------------------------+
//|  Daily Reset                                                     |
//+------------------------------------------------------------------+
void CheckDailyReset()
{
   MqlDateTime now_dt, last_dt;
   TimeToStruct(TimeCurrent(), now_dt);
   TimeToStruct(g_last_reset,  last_dt);

   if(now_dt.day == last_dt.day)
      return;

   //--- New day
   g_trades_today = 0;
   g_day_start_eq = AccountInfoDouble(ACCOUNT_EQUITY);
   g_or_high      = 0.0;
   g_or_low       = 0.0;
   g_or_formed    = false;
   g_or_fired     = false;
   g_gap_checked  = false;
   g_prev_close   = FetchPrevClose();
   g_last_reset   = TimeCurrent();

   //--- Reset scenario flags on Monday
   if(now_dt.day_of_week == 1)
   {
      g_sc_a_fired = false;
      g_sc_b_fired = false;
   }

   PrintFormat("=== New Day | Equity: %.2f | PrevClose: %.2f ===",
               g_day_start_eq, g_prev_close);
}

//+------------------------------------------------------------------+
//|  Daily Loss Limit                                                |
//+------------------------------------------------------------------+
bool DailyLossLimitHit()
{
   if(g_day_start_eq <= 0.0)
      return false;
   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double loss_pct = (g_day_start_eq - equity) / g_day_start_eq * 100.0;
   return (loss_pct >= InpDailyLossLimit);
}

//+------------------------------------------------------------------+
//|  Strategy 1 — Mind the Gap                                       |
//+------------------------------------------------------------------+
void RunGapStrategy(const MqlDateTime &dt)
{
   //--- Only within 5 minutes of either session open
   bool near_london = (dt.hour == InpLondonHour && dt.min >= InpLondonMin &&
                       dt.min <= InpLondonMin + 5);
   bool near_ny     = (dt.hour == InpNYHour     && dt.min >= InpNYMin &&
                       dt.min <= InpNYMin + 5);

   if(!near_london && !near_ny)
      return;

   g_gap_checked = true;

   if(g_prev_close <= 0.0)
      return;

   //--- Current bar open
   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 0, 2, r) < 2)
      return;

   double curr_open = r[1].open;
   double point     = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double gap       = curr_open - g_prev_close;
   double gap_pts   = MathAbs(gap) / point;

   if(gap_pts < InpMinGapPts || gap_pts > InpMaxGapPts)
   {
      PrintFormat("[GAP] %.1f pts — outside [%.0f, %.0f], skipped.",
                  gap_pts, InpMinGapPts, InpMaxGapPts);
      return;
   }

   //--- Fade the gap
   double buf = 5.0 * point;
   double tp  = g_prev_close;
   string dir;
   double entry, sl;

   if(gap > 0.0)
   {
      dir   = "sell";
      entry = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      sl    = curr_open + buf;
   }
   else
   {
      dir   = "buy";
      entry = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      sl    = curr_open - buf;
   }

   if(!EMAFilter(dir))
      return;

   PrintFormat("[GAP] %s %s | Gap: %.1f pts | Entry: %.2f | SL: %.2f | TP: %.2f",
               (dir=="buy") ? "LONG" : "SHORT", _Symbol,
               gap_pts, entry, sl, tp);

   double lots = ComputeLots(entry, sl);
   if(lots > 0.0)
      PlaceOrder(dir, entry, sl, tp, lots, "mind_the_gap");
}

//+------------------------------------------------------------------+
//|  Strategy 2 — Opening Range Breakout                             |
//+------------------------------------------------------------------+
void RunBreakoutStrategy(const MqlDateTime &dt)
{
   if(g_or_fired)
      return;

   int offset = SessionOffset(dt);
   if(offset < 0)
      return;

   //--- Build the opening range once it has closed
   if(!g_or_formed)
   {
      if(offset < InpORMinutes)
         return;   // still forming
      if(!BuildOR(dt))
         return;
   }

   //--- Only trade within session window, after OR is formed
   if(offset <= InpORMinutes || offset > InpSessionWindow)
      return;

   //--- Last closed bar
   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 0, 2, r) < 2)
      return;

   double last_close = r[1].close;
   double candle_sz  = r[1].high - r[1].low;
   double avg_sz     = AvgCandleSize();

   if(candle_sz > InpMaxCandleMult * avg_sz)
   {
      PrintFormat("[BREAKOUT] Skipped — huge candle %.2f > %.1fx avg %.2f",
                  candle_sz, InpMaxCandleMult, avg_sz);
      return;
   }

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);

   //--- LONG breakout
   if(last_close > g_or_high)
   {
      if(!EMAFilter("buy"))
         return;

      double entry = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double sl    = SwingLow() - InpStopBufPts * point;
      if(sl <= 0.0) sl = g_or_low - InpStopBufPts * point;

      PrintFormat("[BREAKOUT] LONG | Entry: %.2f | SL: %.2f | OR: %.2f-%.2f",
                  entry, sl, g_or_low, g_or_high);

      double lots = ComputeLots(entry, sl);
      if(lots > 0.0)
      {
         g_or_fired = true;
         PlaceOrder("buy", entry, sl, 0.0, lots, "breakout");
      }
      return;
   }

   //--- SHORT breakout
   if(last_close < g_or_low)
   {
      if(!EMAFilter("sell"))
         return;

      double entry = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double sl    = SwingHigh() + InpStopBufPts * point;
      if(sl <= 0.0) sl = g_or_high + InpStopBufPts * point;

      PrintFormat("[BREAKOUT] SHORT | Entry: %.2f | SL: %.2f | OR: %.2f-%.2f",
                  entry, sl, g_or_low, g_or_high);

      double lots = ComputeLots(entry, sl);
      if(lots > 0.0)
      {
         g_or_fired = true;
         PlaceOrder("sell", entry, sl, 0.0, lots, "breakout");
      }
   }
}

//+------------------------------------------------------------------+
//|  Build Opening Range from historical bars                        |
//+------------------------------------------------------------------+
bool BuildOR(const MqlDateTime &dt)
{
   MqlDateTime open_dt = dt;
   open_dt.sec = 0;

   //--- Use the active session (London or NY)
   if(SessionOffset(dt) >= 0)
   {
      bool in_london = (dt.hour >  InpLondonHour ||
                       (dt.hour == InpLondonHour && dt.min >= InpLondonMin));
      bool in_ny     = (dt.hour >  InpNYHour ||
                       (dt.hour == InpNYHour     && dt.min >= InpNYMin));

      if(in_london && !(in_ny && InpNYHour < InpLondonHour))
      {
         open_dt.hour = InpLondonHour;
         open_dt.min  = InpLondonMin;
      }
      else
      {
         open_dt.hour = InpNYHour;
         open_dt.min  = InpNYMin;
      }
   }

   datetime t_open = StructToTime(open_dt);
   datetime t_end  = t_open + (datetime)(InpORMinutes * 60);

   MqlRates r[];
   ArraySetAsSeries(r, true);
   int n = CopyRates(_Symbol, PERIOD_CURRENT, t_open, t_end, r);
   if(n <= 0)
   {
      //--- Fallback: use last InpORMinutes bars
      n = CopyRates(_Symbol, PERIOD_CURRENT, 1, InpORMinutes + 5, r);
      if(n <= 0) return false;
   }

   double hi = 0.0, lo = DBL_MAX;
   for(int i = 0; i < n; i++)
   {
      if(r[i].high > hi) hi = r[i].high;
      if(r[i].low  < lo) lo = r[i].low;
   }

   if(hi <= 0.0 || lo >= DBL_MAX || hi <= lo)
      return false;

   g_or_high   = hi;
   g_or_low    = lo;
   g_or_formed = true;

   PrintFormat("[OR] Formed: %.2f - %.2f (%.1f pts)",
               g_or_low, g_or_high,
               (g_or_high - g_or_low) / SymbolInfoDouble(_Symbol, SYMBOL_POINT));
   return true;
}

//+------------------------------------------------------------------+
//|  Strategy 3 — Scenario-Based Swings                              |
//+------------------------------------------------------------------+
void RunScenarioStrategy(const MqlDateTime &dt)
{
   int dow = dt.day_of_week;   // 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);

   //--- Scenario A: Monday high beats Tuesday and Wednesday
   //    Enter SHORT at Wednesday close
   if(InpScenarioA && dow == 3 && !g_sc_a_fired)
   {
      double mon_h = g_w_high[1];
      double tue_h = g_w_high[2];
      double wed_h = g_w_high[3];

      if(mon_h > 0.0 && tue_h > 0.0 && wed_h > 0.0 &&
         mon_h > tue_h && mon_h > wed_h)
      {
         double entry = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double sl    = mon_h + InpStopBufPts * 5.0 * point;

         PrintFormat("[SCENARIO A] Mon %.2f > Tue %.2f & Wed %.2f | SHORT @ %.2f | SL: %.2f",
                     mon_h, tue_h, wed_h, entry, sl);

         double lots = ComputeLots(entry, sl);
         if(lots > 0.0)
         {
            g_sc_a_fired = true;
            PlaceOrder("sell", entry, sl, 0.0, lots, "scenario_A");
         }
      }
   }

   //--- Scenario B: Thursday high beats Friday high
   //    Enter SHORT at Friday close (anticipate Monday gap-down)
   if(InpScenarioB && dow == 5 && !g_sc_b_fired)
   {
      double thu_h = g_w_high[4];
      double fri_h = g_w_high[5];

      if(thu_h > 0.0 && fri_h > 0.0 && thu_h > fri_h)
      {
         double entry = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double sl    = thu_h + InpStopBufPts * 5.0 * point;

         PrintFormat("[SCENARIO B] Thu %.2f > Fri %.2f | SHORT @ %.2f | SL: %.2f",
                     thu_h, fri_h, entry, sl);

         double lots = ComputeLots(entry, sl);
         if(lots > 0.0)
         {
            g_sc_b_fired = true;
            PlaceOrder("sell", entry, sl, 0.0, lots, "scenario_B");
         }
      }
   }
}

//+------------------------------------------------------------------+
//|  Refresh weekly high/low/close from D1 bars                      |
//+------------------------------------------------------------------+
void RefreshWeeklyData()
{
   MqlRates daily[];
   ArraySetAsSeries(daily, true);
   int n = CopyRates(_Symbol, PERIOD_D1, 0, 7, daily);
   if(n < 2) return;

   ArrayInitialize(g_w_high,  0.0);
   ArrayInitialize(g_w_low,   0.0);
   ArrayInitialize(g_w_close, 0.0);

   for(int i = 0; i < n; i++)
   {
      MqlDateTime d;
      TimeToStruct(daily[i].time, d);
      int dow = d.day_of_week;   // 1=Mon .. 5=Fri
      if(dow < 1 || dow > 5) continue;
      g_w_high[dow]  = daily[i].high;
      g_w_low[dow]   = daily[i].low;
      g_w_close[dow] = daily[i].close;
   }
}

//+------------------------------------------------------------------+
//|  Trailing Stop Manager (called every tick)                       |
//+------------------------------------------------------------------+
void ManageTrailingStop()
{
   if(g_r_dist <= 0.0 || g_dir == 0)
      return;

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double cur = (g_dir > 0) ? bid : ask;

   //--- Current R
   double cur_r = (g_dir > 0)
      ? (cur - g_entry) / g_r_dist
      : (g_entry - cur) / g_r_dist;

   if(cur_r < InpTrailR)
      return;

   //--- New stop: lock in (cur_r - 1.0) R from entry
   double lock_r    = MathMax(0.0, cur_r - 1.0);
   double new_stop  = (g_dir > 0)
      ? g_entry + lock_r * g_r_dist
      : g_entry - lock_r * g_r_dist;

   new_stop = NormalizeDouble(new_stop, _Digits);

   //--- Only move stop in the favourable direction
   bool should_move = (g_dir > 0) ? (new_stop > g_stop)
                                   : (new_stop < g_stop);
   if(!should_move) return;

   if(!g_trailing_on)
   {
      g_trailing_on = true;
      PrintFormat("[TRAIL] Activated at %.2fR | New SL: %.5f", cur_r, new_stop);
   }

   ulong ticket = FindPositionTicket();
   if(ticket == 0) return;

   if(PositionSelectByTicket(ticket))
   {
      double tp = PositionGetDouble(POSITION_TP);
      if(InpPaperMode)
         PrintFormat("[PAPER TRAIL] SL moved to %.5f", new_stop);
      else
         g_trade.PositionModify(ticket, new_stop, tp);

      g_stop = new_stop;
   }
}

//+------------------------------------------------------------------+
//|  Place Order                                                     |
//+------------------------------------------------------------------+
void PlaceOrder(string dir, double entry, double sl, double tp,
                double lots, string strategy)
{
   entry = NormalizeDouble(entry, _Digits);
   sl    = NormalizeDouble(sl,    _Digits);
   tp    = (tp > 0.0) ? NormalizeDouble(tp, _Digits) : 0.0;

   if(InpPaperMode)
   {
      PrintFormat("[PAPER %s] %s %.2f lots @ %.2f | SL: %.2f | TP: %.2f | %s",
                  (dir=="buy") ? "BUY" : "SELL",
                  _Symbol, lots, entry, sl, tp, strategy);
   }
   else
   {
      bool ok;
      if(dir == "buy")
         ok = g_trade.Buy(lots, _Symbol, entry, sl, tp,
                          InpComment + "_" + strategy);
      else
         ok = g_trade.Sell(lots, _Symbol, entry, sl, tp,
                           InpComment + "_" + strategy);

      if(!ok)
      {
         PrintFormat("ERROR: %s order failed | retcode: %d | %s",
                     dir, g_trade.ResultRetcode(),
                     g_trade.ResultRetcodeDescription());
         return;
      }
   }

   //--- Record state for trailing stop
   g_entry       = entry;
   g_stop        = sl;
   g_r_dist      = MathAbs(entry - sl);
   g_dir         = (dir == "buy") ? 1 : -1;
   g_trailing_on = false;
   g_trades_today++;

   PrintFormat("[TRADE] %s %s | Lots: %.2f | Entry: %.2f | SL: %.2f | R-dist: %.2f | %s",
               (dir=="buy") ? "LONG" : "SHORT",
               _Symbol, lots, entry, sl, g_r_dist, strategy);
}

//+------------------------------------------------------------------+
//|  Lot Size Calculator — Gold 1% Risk Rule                         |
//|                                                                  |
//|  Gold:  1 std lot = 100 oz                                       |
//|  P&L  = lots x 100 x price_move                                  |
//|  lots = risk_usd / (100 x stop_distance)                         |
//+------------------------------------------------------------------+
double ComputeLots(double entry, double sl)
{
   double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
   double risk_usd  = equity * InpRiskPct / 100.0;
   double stop_dist = MathAbs(entry - sl);

   if(stop_dist <= 0.0)
   {
      Print("ERROR: stop distance is 0 — skipping lot calculation.");
      return 0.0;
   }

   double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(contract <= 0.0) contract = 100.0;   // fallback for XAUUSD

   double lots = risk_usd / (contract * stop_dist);

   double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   lots = MathMax(lots, min_lot);
   lots = MathMin(lots, max_lot);
   lots = MathRound(lots / lot_step) * lot_step;
   lots = NormalizeDouble(lots, 2);

   PrintFormat("LotCalc | Equity: %.2f | Risk: %.2f | StopDist: %.4f | Lots: %.2f",
               equity, risk_usd, stop_dist, lots);
   return lots;
}

//+------------------------------------------------------------------+
//|  EMA Trend Filter                                                |
//+------------------------------------------------------------------+
bool EMAFilter(string dir)
{
   if(!InpEMAOn) return true;
   if(g_ema_handle == INVALID_HANDLE) return true;

   if(CopyBuffer(g_ema_handle, 0, 0, 3, g_ema_buf) < 3) return true;

   double ema_now  = g_ema_buf[0];
   double ema_prev = g_ema_buf[1];
   double price    = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   bool above_ema = (price > ema_now);
   bool slope_up  = (ema_now > ema_prev);

   if(dir == "buy")
   {
      bool ok = above_ema && (!InpEMASlope || slope_up);
      if(!ok)
         PrintFormat("[EMA] LONG blocked | Price: %.2f | EMA: %.2f | SlopeUp: %s",
                     price, ema_now, slope_up ? "Y" : "N");
      return ok;
   }
   else
   {
      bool ok = !above_ema && (!InpEMASlope || !slope_up);
      if(!ok)
         PrintFormat("[EMA] SHORT blocked | Price: %.2f | EMA: %.2f | SlopeDn: %s",
                     price, ema_now, !slope_up ? "Y" : "N");
      return ok;
   }
}

//+------------------------------------------------------------------+
//|  Helpers                                                         |
//+------------------------------------------------------------------+

bool HavePosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetSymbol(i) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC) == (long)InpMagic)
         return true;
   }
   return false;
}

ulong FindPositionTicket()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetSymbol(i) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC) == (long)InpMagic)
         return (ulong)PositionGetInteger(POSITION_TICKET);
   }
   return 0;
}

//--- Minutes since session open, or -1 if outside all sessions
int SessionOffset(const MqlDateTime &dt)
{
   int london_off = (dt.hour - InpLondonHour) * 60 + (dt.min - InpLondonMin);
   if(london_off >= 0 && london_off <= InpSessionWindow)
      return london_off;

   int ny_off = (dt.hour - InpNYHour) * 60 + (dt.min - InpNYMin);
   if(ny_off >= 0 && ny_off <= InpSessionWindow)
      return ny_off;

   return -1;
}

bool IsNearDailyClose(const MqlDateTime &dt)
{
   return (dt.hour == 23 && dt.min >= 50);
}

double FetchPrevClose()
{
   MqlRates r[];
   ArraySetAsSeries(r, true);
   if(CopyRates(_Symbol, PERIOD_CURRENT, 1, 3, r) < 1) return 0.0;
   return r[0].close;
}

double AvgCandleSize()
{
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int n = CopyRates(_Symbol, PERIOD_CURRENT, 1, InpAvgLookback, r);
   if(n < 1) return 1.0;
   double total = 0.0;
   for(int i = 0; i < n; i++)
      total += r[i].high - r[i].low;
   return total / n;
}

double SwingLow()
{
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int n = CopyRates(_Symbol, PERIOD_CURRENT, 1, InpSwingLookback, r);
   if(n < 1) return 0.0;
   double lo = r[0].low;
   for(int i = 1; i < n; i++)
      if(r[i].low < lo) lo = r[i].low;
   return lo;
}

double SwingHigh()
{
   MqlRates r[];
   ArraySetAsSeries(r, true);
   int n = CopyRates(_Symbol, PERIOD_CURRENT, 1, InpSwingLookback, r);
   if(n < 1) return 0.0;
   double hi = r[0].high;
   for(int i = 1; i < n; i++)
      if(r[i].high > hi) hi = r[i].high;
   return hi;
}

//+------------------------------------------------------------------+
//|  OnTradeTransaction — detect when SL/TP closes our position      |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(trans.symbol != _Symbol) return;

   //--- A deal closed our position
   if(trans.deal_type == DEAL_TYPE_BUY || trans.deal_type == DEAL_TYPE_SELL)
   {
      //--- Check if it was a close (entry = position_out)
      HistoryDealSelect(trans.deal);
      long entry_type = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
      if(entry_type != DEAL_ENTRY_OUT) return;

      double exit_price = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
      double profit     = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);

      double r_mult = 0.0;
      if(g_r_dist > 0.0)
      {
         double raw = (g_dir > 0)
            ? (exit_price - g_entry) / g_r_dist
            : (g_entry - exit_price) / g_r_dist;
         r_mult = NormalizeDouble(raw, 2);
      }

      string outcome = (profit > 0.0) ? "WIN" : ((profit < 0.0) ? "LOSS" : "BREAKEVEN");
      PrintFormat("[CLOSED] %s | Exit: %.2f | PnL: %.2f | R: %+.2f",
                  outcome, exit_price, profit, r_mult);

      //--- Reset trade state
      g_entry       = 0.0;
      g_stop        = 0.0;
      g_r_dist      = 0.0;
      g_dir         = 0;
      g_trailing_on = false;
   }
}
