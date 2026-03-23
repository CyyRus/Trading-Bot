#property copyright "Cyyrus XAUUSD Bot - Forexiro-Inspired v1"
#property version "1.00"
#property strict
#include <Trade\Trade.mqh>

// ─── Core Settings (Forexiro-like) ───
input double BaseLotSize        = 0.01;   // Starting lot
input bool   UseMartingale      = false;  // Enable martingale recovery?
input double LotMultiplier      = 2.0;    // Multiplier after loss (e.g. 2.0 = double)
input int    MaxMartingaleSteps = 4;      // Max levels before reset
input double MaxDrawdownPercent = 30.0;   // Stop if drawdown exceeds this %
input int    StopLossPips       = 150;    // Wider for H4
input int    TakeProfitPips     = 300;    // Reward:risk ~2:1
input int    MagicNumber        = 98765;
input int    CooldownSeconds    = 300;    // Cooldown between trades (seconds)

// ─── Filters ───
input double MaxSpreadPoints = 80.0;
input string AllowedSymbol   = "XAUUSD";
input int    TrendMAPeriod   = 50;          // EMA for trend
input ENUM_TIMEFRAMES Timeframe = PERIOD_H4; // Key change: H4 like Forexiro

// ─── Internals ───
CTrade   trade;
datetime lastTradeTime  = 0;
int      trendMAHandle  = INVALID_HANDLE;
double   currentLot     = 0;
int      martingaleLevel = 0;

int OnInit()
{
   if(AllowedSymbol != "" && _Symbol != AllowedSymbol)
   {
      Print("Wrong symbol");
      return(INIT_FAILED);
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetTypeFillingBySymbol(_Symbol);

   trendMAHandle = iMA(_Symbol, Timeframe, TrendMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(trendMAHandle == INVALID_HANDLE) return(INIT_FAILED);

   currentLot = NormalizeLot(BaseLotSize);

   Print("Forexiro-style Bot Init | TF: H4 | Martingale: ", UseMartingale ? "ON" : "OFF");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   if(trendMAHandle != INVALID_HANDLE) IndicatorRelease(trendMAHandle);
}

// ─── OnTradeTransaction: detect close and update martingale ───────────────────
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

   bool wasLoss = (profit <= 0);
   Print("Trade closed | P&L: ", DoubleToString(profit, 2), " | Loss: ", wasLoss ? "YES" : "NO");
   UpdateMartingale(wasLoss);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
int CountOpenTrades()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 &&
         PositionGetInteger(POSITION_MAGIC) == MagicNumber &&
         PositionGetString(POSITION_SYMBOL) == _Symbol)
         count++;
   }
   return count;
}

bool IsSpreadOK()
{
   double spread = SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   return (spread / point) <= MaxSpreadPoints;
}

bool IsCooldownActive()
{
   if(lastTradeTime == 0) return false;
   return (TimeCurrent() - lastTradeTime) < CooldownSeconds;
}

// FIX #1: Use last closed bar (index 1) for close price, not the live candle (index 0)
int GetTrend()
{
   double ma[1];
   ArraySetAsSeries(ma, true);
   if(CopyBuffer(trendMAHandle, 0, 0, 1, ma) != 1) return 0;
   double close = iClose(_Symbol, Timeframe, 1); // FIX: index 1 = last closed bar
   return (close > ma[0]) ? 1 : (close < ma[0]) ? -1 : 0;
}

// FIX #2: Compare closed candles (1 vs 2, not 0 vs 1)
// FIX #3: Use MathAbs() to avoid sign error when prev candle is same direction
bool IsBullishStructure()
{
   double o2 = iOpen (_Symbol, Timeframe, 2); // previous closed candle
   double c2 = iClose(_Symbol, Timeframe, 2);
   double o1 = iOpen (_Symbol, Timeframe, 1); // last closed candle
   double c1 = iClose(_Symbol, Timeframe, 1);

   // Strong bull candle (c1>o1) whose body is >1.5x the body of the prior candle
   if(c1 > o1 && (c1 - o1) > MathAbs(o2 - c2) * 1.5) return true;
   return false;
}

// FIX #2 + #3 applied here too
bool IsBearishStructure()
{
   double o2 = iOpen (_Symbol, Timeframe, 2);
   double c2 = iClose(_Symbol, Timeframe, 2);
   double o1 = iOpen (_Symbol, Timeframe, 1);
   double c1 = iClose(_Symbol, Timeframe, 1);

   // Strong bear candle (o1>c1) whose body is >1.5x the body of the prior candle
   if(c1 < o1 && (o1 - c1) > MathAbs(c2 - o2) * 1.5) return true;
   return false;
}

// FIX #5: Normalize lot to broker step to avoid order rejection
double NormalizeLot(double desiredLot)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(stepLot <= 0) stepLot = 0.01;
   double lot = MathFloor(desiredLot / stepLot) * stepLot;
   return NormalizeDouble(MathMax(minLot, MathMin(maxLot, lot)), 2);
}

// FIX #4 + #5: UpdateMartingale now called via OnTradeTransaction; lot is normalized
void UpdateMartingale(bool wasLoss)
{
   if(!UseMartingale)
   {
      currentLot     = NormalizeLot(BaseLotSize);
      martingaleLevel = 0;
      return;
   }

   if(wasLoss && martingaleLevel < MaxMartingaleSteps)
   {
      martingaleLevel++;
      currentLot = NormalizeLot(BaseLotSize * MathPow(LotMultiplier, martingaleLevel));
      Print("Martingale level ", martingaleLevel, " | New lot: ", currentLot);
   }
   else
   {
      currentLot     = NormalizeLot(BaseLotSize);
      martingaleLevel = 0;
   }

   // FIX #6: Drawdown check — also enforced live in OnTick
   CheckDrawdown();
}

// FIX #6: Standalone drawdown check called both on close and every tick
void CheckDrawdown()
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance <= 0) return;
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   if((balance - equity) / balance * 100.0 > MaxDrawdownPercent)
   {
      Print("Max drawdown reached (", DoubleToString((balance - equity) / balance * 100.0, 1),
            "%) — stopping bot");
      ExpertRemove();
   }
}

void OnTick()
{
   // FIX #6: Live drawdown guard on every tick
   CheckDrawdown();

   if(!IsSpreadOK())         return;
   if(IsCooldownActive())    return;  // FIX: lastTradeTime is now actually used
   if(CountOpenTrades() > 0) return;  // One trade at a time

   int trend = GetTrend();
   if(trend == 0) return;

   bool buySignal  = (trend ==  1 && IsBullishStructure());
   bool sellSignal = (trend == -1 && IsBearishStructure());
   if(!buySignal && !sellSignal) return;

   double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double pip    = point * 10.0;
   double slDist = StopLossPips  * pip;
   double tpDist = TakeProfitPips * pip;
   double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   bool   success = false;

   if(buySignal)
   {
      double sl = NormalizeDouble(ask - slDist, _Digits);
      double tp = NormalizeDouble(ask + tpDist, _Digits);
      success = trade.Buy(currentLot, _Symbol, 0.0, sl, tp, "Buy - Structure");
   }
   else if(sellSignal)
   {
      double sl = NormalizeDouble(bid + slDist, _Digits);
      double tp = NormalizeDouble(bid - tpDist, _Digits);
      success = trade.Sell(currentLot, _Symbol, 0.0, sl, tp, "Sell - Structure");
   }

   if(success)
   {
      lastTradeTime = TimeCurrent();
      Print("Opened ", buySignal ? "BUY" : "SELL", " | Lot: ", currentLot,
            " | Martingale level: ", martingaleLevel);
   }
   else
   {
      Print("Open failed: ", trade.ResultRetcode(), " (", trade.ResultRetcodeDescription(), ")");
   }
}
