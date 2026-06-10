#property copyright "Cyy XAUUSD Survival Scalping Bot"
#property version   "5.0"
#property strict

#include <Trade\Trade.mqh>

//─── Inputs ──────────────────────────────────────────────────────────────────
input group "=== Position Sizing ==="
input bool   UseRiskPercent          = true;   // Size lots by % equity risk (recommended)
input double RiskPercent             = 1.0;    // % of equity to risk per trade (if UseRiskPercent)
input double LotSize                 = 0.01;   // Fixed lot size (used only if UseRiskPercent = false)
input int    MagicNumber             = 12345;  // EA identifier (unique per chart)
input int    MaxOpenTrades           = 1;      // Max simultaneous positions

input group "=== Risk Management ==="
input int    StopLossPips            = 30;     // Stop loss in pips
input double RiskRewardRatio         = 1.8;    // TP = SL x RR (used when QuickTPPips = 0)
input int    QuickTPPips             = 12;     // Quick TP target in pips (0 = use RR ratio)
input int    BreakevenPips           = 5;      // Move SL to entry once profit hits this
input int    TrailStartPips          = 10;     // Begin trailing SL at this profit level
input int    TrailStepPips           = 6;      // Keep SL this many pips behind price
input int    MomentumExitPips        = 8;      // Early-exit if losing >= this AND opposing M5 candle

input group "=== Daily Risk Limits ==="
input double DailyLossLimitPct       = 2.0;    // Pause new trades if equity down this % vs day start
input int    MaxTradesPerDay         = 8;      // Max new entries per day (0 = unlimited)

input group "=== Entry Filters ==="
input int    D1TrendMAPeriod         = 50;     // D1 EMA period (trend gate)
input int    ConfirmCandles          = 2;      // Breakout confirmation candles
input int    PullbackPips            = 10;     // Required pullback depth (normal entry)
input double MaxSpreadPoints         = 50;     // Max allowed spread in points
input string AllowedSymbol           = "XAUUSD";

input group "=== Quick Re-entry (Trend Riding) ==="
input bool   EnableQuickReentry      = true;   // Re-enter quickly after a profitable close
input int    QuickReentrySeconds     = 45;     // Cooldown seconds after profitable close
input int    QuickReentryTimeoutMin  = 5;      // Disarm quick re-entry after this many minutes
input int    NormalCooldownMin       = 15;     // Cooldown minutes after a loss

input group "=== Session Filter ==="
input bool   EnableSessionFilter     = true;   // Only trade during active market sessions
input int    SessionStartHour        = 8;      // UTC hour to start (London open)
input int    SessionEndHour          = 22;     // UTC hour to stop  (NY close)

input group "=== Trailing Stop Tuning ==="
input double MinTrailStepPoints      = 10;     // Min SL move (points) before re-modifying position

input group "=== Misc ==="
input bool   EnableVolPause          = false;  // Pause on volatile hour-edge minutes
input int    MaxSlippagePoints       = 30;     // Max slippage on market orders
input double TargetBalance           = 5000.0; // Auto-stop when balance reaches this

//─── Globals ─────────────────────────────────────────────────────────────────
CTrade   trade;
datetime lastTradeTime      = 0;
datetime lastAlivePrint     = 0;
datetime lastDiagBar        = 0;
datetime lastExitBar        = 0;
int      d1MAHandle         = INVALID_HANDLE;

// Quick re-entry state
bool     quickReentryArmed  = false;
datetime quickReentryArmedAt = 0;

// Daily risk tracking
double   dayStartEquity     = 0.0;
datetime currentDayStart    = 0;
int      tradesToday        = 0;
bool     dailyLimitHit      = false;

//─── OnInit ──────────────────────────────────────────────────────────────────
int OnInit()
{
   if(AllowedSymbol != "" && _Symbol != AllowedSymbol)
   {
      Print("ERROR: EA designed for ", AllowedSymbol, " but attached to ", _Symbol);
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaxSlippagePoints);   // slippage protection
   trade.SetTypeFilling(DetectFillingMode());

   d1MAHandle = iMA(_Symbol, PERIOD_D1, D1TrendMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(d1MAHandle == INVALID_HANDLE)
   {
      Print("ERROR: Failed to create D1 MA handle");
      return INIT_FAILED;
   }

   lastAlivePrint  = TimeCurrent();
   currentDayStart = StartOfDay(TimeCurrent());
   dayStartEquity  = AccountInfoDouble(ACCOUNT_EQUITY);

   double effLot = NormalizeLot(LotSize);

   Print("=================================================================");
   Print("Cyy Scalping Bot v5.0 | Symbol: ", _Symbol);
   Print("Balance     : ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   if(UseRiskPercent)
      Print("Sizing      : ", DoubleToString(RiskPercent, 2), "% equity risk per trade");
   else
      Print("Sizing      : ", DoubleToString(effLot, 3), " fixed lots",
            " (min=", DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), 3),
            " step=", DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP), 3), ")");
   Print("QuickTP     : ", QuickTPPips > 0 ? (string)QuickTPPips + " pips" : "RR ratio (" + DoubleToString(RiskRewardRatio,1) + "x)");
   Print("ReEntry     : ", EnableQuickReentry ? (string)QuickReentrySeconds + "s after profit | timeout " + (string)QuickReentryTimeoutMin + "min" : "OFF");
   Print("Session     : ", EnableSessionFilter ? (string)SessionStartHour + ":00-" + (string)SessionEndHour + ":00 UTC" : "24/7");
   Print("Breakeven   : +", BreakevenPips, "p | Trail: +", TrailStartPips, "p start / ", TrailStepPips, "p step");
   Print("DailyLimit  : -", DoubleToString(DailyLossLimitPct, 2), "% | MaxTrades/day: ",
         MaxTradesPerDay == 0 ? "unlimited" : (string)MaxTradesPerDay);
   Print("Slippage    : max ", MaxSlippagePoints, " points");
   if(QuickTPPips > 0 && QuickTPPips < StopLossPips)
      Print("NOTE: TP(", QuickTPPips, "p) < SL(", StopLossPips, "p) — needs a high win rate;",
            " momentum exit (Layer 3) is relied on to cap losers early.");
   Print("=================================================================");
   return INIT_SUCCEEDED;
}

//─── OnDeinit ────────────────────────────────────────────────────────────────
void OnDeinit(const int reason)
{
   if(d1MAHandle != INVALID_HANDLE) IndicatorRelease(d1MAHandle);
   Print("Bot stopped | Reason: ", reason,
         " | Balance: ", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
}

//─── OnTradeTransaction ──────────────────────────────────────────────────────
// Fires when a deal is executed. Used to detect profitable closes and arm
// the quick re-entry mode so the bot jumps straight back into the trend.
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(!HistoryDealSelect(trans.deal))           return;
   if(HistoryDealGetInteger(trans.deal, DEAL_MAGIC)  != MagicNumber)    return;
   if(HistoryDealGetInteger(trans.deal, DEAL_ENTRY)  != DEAL_ENTRY_OUT) return;

   double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT)
                 + HistoryDealGetDouble(trans.deal, DEAL_SWAP)
                 + HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);

   if(profit > 0 && EnableQuickReentry)
   {
      quickReentryArmed   = true;
      quickReentryArmedAt = TimeCurrent();
      Print("Profit close | P&L: +", DoubleToString(profit, 2),
            " | Quick re-entry armed | cooldown: ", QuickReentrySeconds, "s");
   }
   else
   {
      quickReentryArmed = false;
      Print("Loss/BE close | P&L: ", DoubleToString(profit, 2),
            " | Normal cooldown: ", NormalCooldownMin, "min");
   }
}

//─── Helpers ─────────────────────────────────────────────────────────────────
int CountOpenTrades()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(PositionGetTicket(i) > 0 &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         count++;
   }
   return count;
}

bool IsCooldownActive()
{
   if(lastTradeTime == 0) return false;
   int cooldownSec  = quickReentryArmed ? QuickReentrySeconds : (NormalCooldownMin * 60);
   int secondsLeft  = cooldownSec - (int)(TimeCurrent() - lastTradeTime);
   if(secondsLeft > 0)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 30)
      {
         Print("Cooldown: ", secondsLeft, "s | mode: ", quickReentryArmed ? "quick" : "normal");
         lastLog = TimeCurrent();
      }
      return true;
   }
   return false;
}

bool IsWithinSession()
{
   if(!EnableSessionFilter) return true;
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   if(tm.hour >= SessionStartHour && tm.hour < SessionEndHour) return true;
   static datetime lastLog = 0;
   if(TimeCurrent() - lastLog >= 3600)
   {
      Print("Outside session (UTC ", tm.hour, ":xx) — waiting for ", SessionStartHour, ":00");
      lastLog = TimeCurrent();
   }
   return false;
}

bool IsVolatileTime()
{
   if(!EnableVolPause) return false;
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   if(tm.min < 2 || tm.min > 58)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Volatile hour edge — pausing");
         lastLog = TimeCurrent();
      }
      return true;
   }
   return false;
}

bool IsSpreadAcceptable()
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point < 1e-10) return false;
   double spreadPoints = (SymbolInfoDouble(_Symbol, SYMBOL_ASK) -
                          SymbolInfoDouble(_Symbol, SYMBOL_BID)) / point;
   if(spreadPoints > MaxSpreadPoints)
   {
      static datetime lastLog = 0;
      if(TimeCurrent() - lastLog >= 60)
      {
         Print("Spread too wide: ", DoubleToString(spreadPoints, 1), " pts");
         lastLog = TimeCurrent();
      }
      return false;
   }
   return true;
}

double NormalizeLot(double desiredLot)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(stepLot <= 0) stepLot = 0.01;
   double lot = MathFloor(desiredLot / stepLot) * stepLot;
   lot = MathMax(minLot, MathMin(maxLot, lot));
   return NormalizeDouble(lot, 2);
}

// Pick the order filling mode the broker actually supports for this symbol.
// Avoids "Unsupported filling mode" errors on brokers that don't allow IOC/FOK.
ENUM_ORDER_TYPE_FILLING DetectFillingMode()
{
   int filling = (int)SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

datetime StartOfDay(datetime t)
{
   MqlDateTime tm;
   TimeToStruct(t, tm);
   tm.hour = 0; tm.min = 0; tm.sec = 0;
   return StructToTime(tm);
}

// A new SL must be at least SYMBOL_TRADE_STOPS_LEVEL points away from the
// current price, otherwise the broker rejects the modify request.
bool IsStopDistanceValid(double refPrice, double stopPrice)
{
   long stopsLevel = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stopsLevel <= 0) return true;
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   return MathAbs(refPrice - stopPrice) >= stopsLevel * point;
}

//─── Daily Risk Tracking ─────────────────────────────────────────────────────
void CheckDailyReset()
{
   datetime today = StartOfDay(TimeCurrent());
   if(today == currentDayStart) return;

   currentDayStart = today;
   dayStartEquity  = AccountInfoDouble(ACCOUNT_EQUITY);
   tradesToday     = 0;
   dailyLimitHit   = false;
   Print("=== New trading day | Equity reset point: ", DoubleToString(dayStartEquity, 2), " ===");
}

bool IsDailyLossLimitHit()
{
   if(dailyLimitHit) return true;
   if(dayStartEquity <= 0) return false;

   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double lossPct = (dayStartEquity - equity) / dayStartEquity * 100.0;
   if(lossPct >= DailyLossLimitPct)
   {
      dailyLimitHit = true;
      Print("DAILY LOSS LIMIT HIT (-", DoubleToString(lossPct, 2),
            "%) — pausing new entries until next day");
   }
   return dailyLimitHit;
}

//─── Position Sizing ─────────────────────────────────────────────────────────
// Risk-percent sizing for Gold: 1 standard lot = 100 oz, so a $1 move on
// 1 lot = $100 P&L. lots = risk_usd / (stop_distance_price x contract_size)
double CalcLotSize(double slDistPrice)
{
   if(!UseRiskPercent || slDistPrice <= 0)
      return NormalizeLot(LotSize);

   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskUsd  = equity * RiskPercent / 100.0;
   double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(contract <= 0) contract = 100.0;

   return NormalizeLot(riskUsd / (slDistPrice * contract));
}

//─── Trade Management ────────────────────────────────────────────────────────
// Three exit layers run every tick while a position is open:
//   1. Breakeven  — lock SL at entry once floating profit >= BreakevenPips
//   2. Trailing   — trail SL once floating profit >= TrailStartPips
//   3. Momentum   — close early if underwater >= MomentumExitPips + opposing M5 candle
void ManageOpenTrades()
{
   double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double pip    = point * 10;
   bool   newBar = false;
   datetime barTime = iTime(_Symbol, PERIOD_M5, 1);
   if(barTime != lastExitBar) { lastExitBar = barTime; newBar = true; }

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)    continue;

      long   posType   = PositionGetInteger(POSITION_TYPE);
      double entry     = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl        = PositionGetDouble(POSITION_SL);
      double tp        = PositionGetDouble(POSITION_TP);
      double bid       = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask       = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double floatPips = (posType == POSITION_TYPE_BUY)
                         ? (bid - entry) / pip
                         : (entry - ask) / pip;

      // ── Layer 1: Breakeven ───────────────────────────────────────────────
      if(floatPips >= BreakevenPips)
      {
         double beSL     = (posType == POSITION_TYPE_BUY)
                           ? NormalizeDouble(entry + pip, _Digits)   // 1 pip above entry
                           : NormalizeDouble(entry - pip, _Digits);  // 1 pip below entry
         bool needsMove  = (posType == POSITION_TYPE_BUY  && (sl < 1e-10 || sl < entry))
                        || (posType == POSITION_TYPE_SELL && (sl < 1e-10 || sl > entry));
         if(needsMove)
         {
            double refPrice = (posType == POSITION_TYPE_BUY) ? bid : ask;
            if(!IsStopDistanceValid(refPrice, beSL))
            {
               // Too close to current price — broker would reject; retry next tick.
            }
            else if(trade.PositionModify(ticket, beSL, tp))
               Print("Breakeven | #", ticket, " SL:", sl, "->", beSL,
                     " float:+", DoubleToString(floatPips, 1), "p");
            else
               Print("Breakeven failed | #", ticket, " err:", GetLastError());
         }
      }

      // ── Layer 2: Trailing stop ───────────────────────────────────────────
      if(floatPips >= TrailStartPips)
      {
         double trailDist = TrailStepPips * pip;
         double newSL     = (posType == POSITION_TYPE_BUY)
                            ? NormalizeDouble(bid - trailDist, _Digits)
                            : NormalizeDouble(ask + trailDist, _Digits);

         // BUY  improves when new SL is higher (further from loss)
         // SELL improves when new SL is lower  (further from loss)
         // Use 1e-10 epsilon instead of == 0 to handle floating-point edge cases
         bool improves = (posType == POSITION_TYPE_BUY  && (sl < 1e-10 || newSL > sl))
                      || (posType == POSITION_TYPE_SELL && (sl < 1e-10 || newSL < sl));

         // Only re-modify once the SL has moved by a meaningful amount —
         // avoids spamming PositionModify on every tick of a slow drift.
         bool bigEnough = (sl < 1e-10) || (MathAbs(newSL - sl) >= MinTrailStepPoints * point);

         if(improves && bigEnough)
         {
            double refPrice = (posType == POSITION_TYPE_BUY) ? bid : ask;
            if(!IsStopDistanceValid(refPrice, newSL))
            {
               // Too close to current price — broker would reject; retry next tick.
            }
            else if(trade.PositionModify(ticket, newSL, tp))
               Print("Trail | #", ticket, " SL:", sl, "->", newSL,
                     " float:+", DoubleToString(floatPips, 1), "p");
            else
               Print("Trail failed | #", ticket, " err:", GetLastError());
         }
      }

      // ── Layer 3: Momentum exit (once per new M5 candle) ──────────────────
      if(newBar && floatPips <= -MomentumExitPips)
      {
         double cClose = iClose(_Symbol, PERIOD_M5, 1);
         double cOpen  = iOpen (_Symbol, PERIOD_M5, 1);
         bool opposes  = (posType == POSITION_TYPE_BUY  && cClose < cOpen)
                      || (posType == POSITION_TYPE_SELL && cClose > cOpen);
         if(opposes)
         {
            if(trade.PositionClose(ticket))
               Print("Momentum exit | #", ticket,
                     " float:", DoubleToString(floatPips, 1), "p | opposing candle");
            else
               Print("Momentum exit failed | #", ticket, " err:", GetLastError());
         }
      }
   }
}

//─── Signal functions ────────────────────────────────────────────────────────
int GetD1TrendDirection()
{
   double ma[];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(d1MAHandle, 0, 0, 1, ma) <= 0) return 0;
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid > ma[0]) return  1;
   if(bid < ma[0]) return -1;
   return 0;
}

// Two consecutive M5 candles in the same direction = momentum confirmed
int GetM5MomentumDirection()
{
   double c1 = iClose(_Symbol, PERIOD_M5, 1), o1 = iOpen(_Symbol, PERIOD_M5, 1);
   double c2 = iClose(_Symbol, PERIOD_M5, 2), o2 = iOpen(_Symbol, PERIOD_M5, 2);
   if(c1 > o1 && c2 > o2) return  1;
   if(c1 < o1 && c2 < o2) return -1;
   return 0;
}

// Breakout: ConfirmCandles consecutive same-direction M5 candles (starting at bar 2)
int GetBreakoutSignal()
{
   int bulls = 0, bears = 0;
   for(int i = 2; i <= 2 + ConfirmCandles - 1; i++)
   {
      double o = iOpen (_Symbol, PERIOD_M5, i);
      double c = iClose(_Symbol, PERIOD_M5, i);
      if(c > o) bulls++;
      else if(c < o) bears++;
   }
   if(bulls == ConfirmCandles) return  1;
   if(bears == ConfirmCandles) return -1;
   return 0;
}

// Pullback: price must retrace at least PullbackPips from breakout candle close
bool IsPullbackAfterBreakout(int signal)
{
   double pip           = SymbolInfoDouble(_Symbol, SYMBOL_POINT) * 10;
   double close1        = iClose(_Symbol, PERIOD_M5, 1);
   double closeBreakout = iClose(_Symbol, PERIOD_M5, 2);
   if(signal ==  1) return (closeBreakout - close1) > PullbackPips * pip;
   if(signal == -1) return (close1 - closeBreakout) > PullbackPips * pip;
   return false;
}

//─── Place Trade ─────────────────────────────────────────────────────────────
bool PlaceTrade(int direction, string label)
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(point < 1e-10) return false;

   double slDist = StopLossPips * point * 10;
   double tpDist = (QuickTPPips > 0)
                   ? QuickTPPips * point * 10
                   : StopLossPips * RiskRewardRatio * point * 10;

   double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double effLot = CalcLotSize(slDist);
   bool   ok     = false;

   if(effLot <= 0)
   {
      Print("PlaceTrade aborted | computed lot size <= 0");
      return false;
   }

   if(direction == 1)
   {
      double sl = NormalizeDouble(ask - slDist, _Digits);
      double tp = NormalizeDouble(ask + tpDist, _Digits);
      ok = trade.Buy(effLot, _Symbol, ask, sl, tp, label);
      Print(ok ? "BUY" : "BUY FAILED",
            " | lot:", effLot, " ask:", ask, " SL:", sl, " TP:", tp,
            " (", QuickTPPips > 0 ? (string)QuickTPPips + "p" : "RR", ")",
            ok ? "" : " err:" + (string)GetLastError());
   }
   else
   {
      double sl = NormalizeDouble(bid + slDist, _Digits);
      double tp = NormalizeDouble(bid - tpDist, _Digits);
      ok = trade.Sell(effLot, _Symbol, bid, sl, tp, label);
      Print(ok ? "SELL" : "SELL FAILED",
            " | lot:", effLot, " bid:", bid, " SL:", sl, " TP:", tp,
            " (", QuickTPPips > 0 ? (string)QuickTPPips + "p" : "RR", ")",
            ok ? "" : " err:" + (string)GetLastError());
   }

   if(ok) tradesToday++;
   return ok;
}

//─── OnTick ──────────────────────────────────────────────────────────────────
void OnTick()
{
   datetime now = TimeCurrent();

   CheckDailyReset();

   // Hourly heartbeat
   if(now - lastAlivePrint >= 3600)
   {
      double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
      double dayPnlPct = (dayStartEquity > 0) ? (equity - dayStartEquity) / dayStartEquity * 100.0 : 0.0;
      Print("Alive | Bal:", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
            " Eq:", DoubleToString(equity, 2),
            " Open:", CountOpenTrades(),
            " TradesToday:", tradesToday, "/", MaxTradesPerDay == 0 ? "unlimited" : (string)MaxTradesPerDay,
            " DayPnL:", DoubleToString(dayPnlPct, 2), "%",
            " ReEntry:", quickReentryArmed ? "ARMED" : "off");
      lastAlivePrint = now;
   }

   // Target balance check
   if(AccountInfoDouble(ACCOUNT_BALANCE) >= TargetBalance)
   {
      Print("TARGET REACHED | Balance: ",
            DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
      ExpertRemove();
      return;
   }

   ManageOpenTrades();

   if(CountOpenTrades() >= MaxOpenTrades) return;
   if(!IsWithinSession())                 return;
   if(IsVolatileTime())                   return;
   if(IsCooldownActive())                 return;
   if(!IsSpreadAcceptable())              return;
   if(IsDailyLossLimitHit())              return;
   if(MaxTradesPerDay > 0 && tradesToday >= MaxTradesPerDay) return;

   // Quick re-entry timeout: disarm if we've been waiting too long
   if(quickReentryArmed &&
      (now - quickReentryArmedAt) > QuickReentryTimeoutMin * 60)
   {
      quickReentryArmed = false;
      Print("Quick re-entry timed out after ", QuickReentryTimeoutMin,
            " min — returning to normal entry mode");
   }

   int d1Trend = GetD1TrendDirection();
   if(d1Trend == 0) return;

   // ── Path A: Quick re-entry (trend riding after profitable close) ─────────
   // Requirements: D1 trend still active + M5 momentum confirms direction.
   // No pullback required — we're already in the trend and just took a quick profit.
   if(quickReentryArmed && EnableQuickReentry)
   {
      int m5Mom = GetM5MomentumDirection();
      if(m5Mom != 0 && m5Mom == d1Trend)
      {
         Print("Quick re-entry | D1:", d1Trend, " M5:", m5Mom);
         if(PlaceTrade(d1Trend, "QuickReEntry"))
         {
            lastTradeTime     = now;
            quickReentryArmed = false;
         }
      }
      // While armed, skip the normal entry path — wait for momentum to align
      return;
   }

   // ── Path B: Normal entry (breakout + pullback + D1 trend) ────────────────
   int  signal   = GetBreakoutSignal();
   bool pullback = (signal != 0) && IsPullbackAfterBreakout(signal);

   // Per-candle diagnostic log
   datetime barTime = iTime(_Symbol, PERIOD_M5, 1);
   if(barTime != lastDiagBar)
   {
      lastDiagBar = barTime;
      string why;
      if     (signal == 0)       why = "No breakout (mixed candles)";
      else if(d1Trend != signal) why = "D1 blocks (bo=" + (string)signal + " D1=" + (string)d1Trend + ")";
      else if(!pullback)         why = "Pullback too small (<" + (string)PullbackPips + "p)";
      else                       why = "ALL CLEAR — entering";
      Print("Diag | bo:", signal, " D1:", d1Trend, " pb:", pullback, " | ", why);
   }

   if(signal == 0)       return;
   if(d1Trend != signal) return;
   if(!pullback)         return;

   if(PlaceTrade(signal, "Pullback"))
      lastTradeTime = now;
}
