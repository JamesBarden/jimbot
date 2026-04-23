from dataclasses import dataclass, field
from typing import Optional

SUIT_MAP = {
    "♠": "s", "spades": "s", "s": "s",
    "♥": "h", "hearts": "h", "h": "h",
    "♦": "d", "diamonds": "d", "d": "d",
    "♣": "c", "clubs": "c", "c": "c",
}

RANK_MAP = {
    "10": "T", "A": "A", "K": "K", "Q": "Q", "J": "J", "T": "T",
    "9": "9", "8": "8", "7": "7", "6": "6", "5": "5", "4": "4",
    "3": "3", "2": "2",
}


@dataclass
class Card:
    rank: str   # '2'-'9', 'T', 'J', 'Q', 'K', 'A'
    suit: str   # 's', 'h', 'd', 'c'

    @staticmethod
    def from_pokernow(raw_rank: str, raw_suit: str) -> "Card":
        rank = RANK_MAP.get(raw_rank.strip().upper(), raw_rank.strip().upper())
        suit = SUIT_MAP.get(raw_suit.strip().lower(), raw_suit.strip().lower())
        return Card(rank=rank, suit=suit)

    def to_treys(self) -> str:
        return self.rank + self.suit

    def __str__(self):
        return f"{self.rank}{self.suit}"

    def __repr__(self):
        return str(self)


@dataclass
class GameState:
    hole_cards: list        # list[Card], length 2
    board_cards: list       # list[Card], 0/3/4/5 cards
    pot: float              # total pot including current-round bets
    to_call: float          # amount to call (0 if check available)
    my_stack: float
    big_blind: float
    num_opponents: int      # active opponents (not folded)
    phase: str              # 'preflop', 'flop', 'turn', 'river'
    is_my_turn: bool
    can_check: bool

    def pot_odds(self) -> float:
        if self.to_call == 0:
            return 0.0
        return self.to_call / (self.pot + self.to_call)

    def __str__(self):
        hand = " ".join(str(c) for c in self.hole_cards)
        board = " ".join(str(c) for c in self.board_cards) if self.board_cards else "-"
        return (
            f"[{self.phase.upper()}] Hand: {hand}  Board: {board}  "
            f"Pot: {self.pot}  ToCall: {self.to_call}  Stack: {self.my_stack}  "
            f"Opponents: {self.num_opponents}"
        )
