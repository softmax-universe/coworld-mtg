//! Explicit hidden-state assumptions used by the partial-observation fitter.
//!
//! Setup fixes a complete initial ordering. Subsequent transitions, including
//! opening draws and mulligan decisions, are performed by the pinned engine.

use super::*;
use phase_engine::game::engine::start_game_with_starting_player;
use phase_engine::types::ability::TargetRef;
use std::collections::BTreeMap;

/// Read one actual combat-damage hit from a production action's event stream.
///
/// This native engine diagnostic does not establish equivalence to any external
/// dataset field, including 17Lands `combat_damage_taken`.
///
/// Phase emits `DamageDealt` after damage replacement/prevention and uses the
/// actual recipient, including redirection. Its amount is damage dealt, which
/// can differ from life lost. Aggregate combat events and life changes must not
/// be added to this value. Sum each returned hit once within its turn; repeated
/// sources across double-strike steps or extra combats are separate hits.
pub fn combat_damage_to_player(event: &GameEvent) -> Option<(u8, u32)> {
    match event {
        GameEvent::DamageDealt {
            target: TargetRef::Player(player),
            amount,
            is_combat: true,
            ..
        } => Some((player.0, *amount)),
        _ => None,
    }
}

impl PhaseRuntime {
    /// Resolve a known name to the exact face name used by the Phase corpus.
    pub fn canonical_card_name(&self, name: &str) -> Option<String> {
        self.cards
            .get_face_by_name(name)
            .map(|face| face.name.clone())
    }

    /// Start a fitting game with a fixed starter and complete initial orders.
    ///
    /// Each `libraries` entry is top-to-bottom *before the opening draw*, so
    /// its first seven cards become that player's initial hand. The order must
    /// contain the complete corresponding deck multiset. These fixed orders
    /// are reconstruction assumptions, not evidence about unobserved cards.
    ///
    /// Phase still offers its normal mulligan choices. A fitter constrained by
    /// recorded zero mulligans must admit only Phase's offered Keep decisions.
    /// Draws and other random outcomes use Phase's seeded RNG normally, but a
    /// shuffle submitted through `submit_for_fitting` keeps the remaining
    /// cards in this order.
    pub fn new_limited_game_for_fitting(
        &self,
        decks: [Vec<String>; 2],
        libraries: [Vec<String>; 2],
        starting_player: u8,
        seed: u64,
    ) -> Result<(PhaseGame, ActionResult), BridgeError> {
        if starting_player > 1 {
            return Err(BridgeError::FittingSetup(format!(
                "starting player must be seat 0 or 1, received {starting_player}"
            )));
        }
        let mut requested_names: [Vec<String>; 2] = Default::default();
        for seat in 0..2 {
            let unknown = decks[seat]
                .iter()
                .filter(|name| self.canonical_card_name(name).is_none())
                .cloned()
                .collect::<Vec<_>>();
            if !unknown.is_empty() {
                return Err(BridgeError::UnknownCards {
                    seat,
                    names: unknown,
                });
            }
            let canonical_deck = decks[seat]
                .iter()
                .map(|name| self.canonical_card_name(name).expect("validated deck name"))
                .collect::<Vec<_>>();
            requested_names[seat] = libraries[seat]
                .iter()
                .map(|name| {
                    self.canonical_card_name(name).ok_or_else(|| {
                        BridgeError::FittingSetup(format!(
                            "seat {seat} library contains unknown card {name:?}"
                        ))
                    })
                })
                .collect::<Result<_, _>>()?;
            if name_counts(&canonical_deck) != name_counts(&requested_names[seat]) {
                return Err(BridgeError::FittingSetup(format!(
                    "seat {seat} library order must contain the complete deck multiset"
                )));
            }
        }

        let deck_list = PhaseDeckList {
            player: PlayerDeckList {
                main_deck: decks[0].clone(),
                ..PlayerDeckList::default()
            },
            opponent: PlayerDeckList {
                main_deck: decks[1].clone(),
                ..PlayerDeckList::default()
            },
            ..PhaseDeckList::default()
        };
        let payload = resolve_deck_list(
            &self.cards,
            &phase_engine::database::combo_table::ComboTable::default(),
            &deck_list,
        );
        let mut state = GameState::new(FormatConfig::limited(), 2, seed);
        load_and_hydrate_decks(&mut state, &payload, Some(&self.cards));
        state.all_card_names = self.cards.card_names().into();

        // Probe the shuffle's permutation on a clone of the exact pregame RNG
        // state. Reorder the original input by its inverse, then let Phase run
        // opening draws normally. Rewriting hand/library lists after starting
        // would leave zone-change events and object incarnation state stale.
        let mut probe = state.clone();
        start_game_with_starting_player(&mut probe, PlayerId(starting_player));
        let mut desired_orders: [Vec<ObjectId>; 2] = Default::default();
        for seat in 0..2 {
            let original = state.players[seat]
                .library
                .iter()
                .copied()
                .collect::<Vec<_>>();
            let shuffled = initial_order(&probe, seat);
            if original.len() != shuffled.len() || original.len() != requested_names[seat].len() {
                return Err(BridgeError::FittingSetup(format!(
                    "seat {seat} pregame setup changed the deck multiset"
                )));
            }
            let mut remaining = original.clone();
            for name in &requested_names[seat] {
                let index = remaining
                    .iter()
                    .position(|id| {
                        state
                            .objects
                            .get(id)
                            .is_some_and(|object| object.name == *name)
                    })
                    .ok_or_else(|| {
                        BridgeError::FittingSetup(format!(
                            "seat {seat} hydrated deck does not contain requested card {name:?}"
                        ))
                    })?;
                desired_orders[seat].push(remaining.remove(index));
            }
            let mut reordered = original.clone();
            for (position, id) in shuffled.iter().enumerate() {
                let source_position = original
                    .iter()
                    .position(|original_id| original_id == id)
                    .ok_or_else(|| {
                        BridgeError::FittingSetup(format!(
                            "seat {seat} pregame setup changed a card's object identity"
                        ))
                    })?;
                reordered[source_position] = desired_orders[seat][position];
            }
            state.players[seat].library = reordered.into_iter().collect();
        }
        let initial = start_game_with_starting_player(&mut state, PlayerId(starting_player));
        for (seat, expected) in desired_orders.iter().enumerate() {
            if initial_order(&state, seat) != *expected {
                return Err(BridgeError::FittingSetup(format!(
                    "seat {seat} pregame shuffle did not preserve the requested order"
                )));
            }
        }
        Ok((
            PhaseGame {
                state,
                cards: self.cards.clone(),
            },
            initial,
        ))
    }
}

/// A pending unconstrained library search owned by one seat: its listed
/// candidates, how many to find, and whether finding fewer is permitted.
struct PendingSearch {
    cards: Vec<ObjectId>,
    count: usize,
    allows_partial_find: bool,
}

impl PhaseGame {
    /// Branch an authoritative search state, including its current RNG stream.
    pub fn fork(&self) -> Self {
        Self {
            state: self.state.clone(),
            cards: self.cards.clone(),
        }
    }

    /// Serialized identity of this state for recognizing a repeated search state.
    ///
    /// Phase records each cancelled cast in `cancelled_casts`, a guard against
    /// its own AI looping cast→cancel→recast. No rules code or legal-action
    /// listing reads it, and it clears on the next priority pass or land play,
    /// so states differing only there offer the same actions and futures. With
    /// it excluded, backing out of a cast returns to the state it left.
    pub fn fitting_identity_json(&self) -> Result<String, BridgeError> {
        if self.state.cancelled_casts.is_empty() {
            return self.checkpoint_json();
        }
        let mut game = self.fork();
        game.state.cancelled_casts.clear();
        game.checkpoint_json()
    }

    fn pending_search(&self, seat: u8) -> Option<PendingSearch> {
        let waiting = serde_json::to_value(&self.state.waiting_for).ok()?;
        let data = waiting
            .get("data")
            .filter(|_| waiting["type"] == "SearchChoice")?;
        // Only an unconstrained choice is fully described by its card list and
        // count; any other constraint is left to Phase's own offered actions.
        if data["player"].as_u64() != Some(u64::from(seat)) || data["constraint"]["type"] != "None"
        {
            return None;
        }
        Some(PendingSearch {
            cards: serde_json::from_value(data["cards"].clone()).ok()?,
            count: usize::try_from(data["count"].as_u64()?).ok()?,
            allows_partial_find: data["allows_partial_find"].as_bool()?,
        })
    }

    /// Every answer a pending unconstrained library search admits, one per
    /// multiset of card names.
    ///
    /// Phase lists the search's candidate cards but offers a single
    /// representative selection among them, so the remaining candidates are
    /// unreachable through `legal_actions`. Same-named library cards are
    /// interchangeable here, so each name multiset is selected once, using the
    /// earliest listed copies.
    pub fn search_choice_selections(&self, seat: u8) -> Vec<GameAction> {
        let Some(search) = self.pending_search(seat) else {
            return vec![];
        };
        let mut groups: Vec<(String, Vec<ObjectId>)> = vec![];
        for id in &search.cards {
            let Some(object) = self.state.objects.get(id) else {
                return vec![];
            };
            match groups.iter_mut().find(|(name, _)| *name == object.name) {
                Some((_, ids)) => ids.push(*id),
                None => groups.push((object.name.clone(), vec![*id])),
            }
        }
        let most = search.count.min(search.cards.len());
        let fewest = if search.allows_partial_find { 0 } else { most };
        let mut result = vec![];
        for size in fewest..=most {
            select_by_name(&groups, size, &mut vec![], &mut result);
        }
        result
    }

    /// Submit an offered action, or an answer `search_choice_selections` admits.
    ///
    /// A library shuffle during the game keeps the reconstructed name order of
    /// the cards that remain, as `keep_order_through_shuffles` describes.
    pub fn submit_for_fitting(
        &mut self,
        seat: u8,
        action: GameAction,
    ) -> Result<ActionResult, BridgeError> {
        let admitted = match (&action, self.pending_search(seat)) {
            (GameAction::SelectCards { cards }, Some(search)) => search.admits(cards),
            _ => false,
        };
        let before = self.library_names();
        let result = if admitted {
            apply(&mut self.state, PlayerId(seat), action)
                .map_err(|error| BridgeError::Action(error.to_string()))?
        } else {
            self.submit(seat, action)?
        };
        self.keep_order_through_shuffles(&before, &result.events);
        Ok(result)
    }

    fn library_names(&self) -> Vec<Vec<String>> {
        self.state
            .players
            .iter()
            .map(|player| {
                player
                    .library
                    .iter()
                    .map(|id| self.state.objects[id].name.clone())
                    .collect()
            })
            .collect()
    }

    /// Reorder each library Phase shuffled so its cards follow the name order
    /// it had before the action.
    ///
    /// The fitting setup fixes one hidden library order so recorded draws come
    /// in recorded order. A shuffle's outcome is just as hidden, and Phase's
    /// seeded permutation would discard that hypothesis for every later draw.
    /// Any order is a possible shuffle outcome, so the reconstruction keeps the
    /// one consistent with its setup: same-named cards are interchangeable,
    /// cards that left take the deepest slot of their name, and cards that
    /// joined the library go to the bottom in Phase's order.
    fn keep_order_through_shuffles(&mut self, before: &[Vec<String>], events: &[GameEvent]) {
        for event in events {
            let value = serde_json::to_value(event).unwrap_or_default();
            if value["type"] != "PlayerPerformedAction"
                || value["data"]["action"] != "ShuffledLibrary"
            {
                continue;
            }
            let Some(seat) = value["data"]["player_id"].as_u64() else {
                continue;
            };
            let (Some(player), Some(previous)) = (
                self.state.players.get(seat as usize),
                before.get(seat as usize),
            ) else {
                continue;
            };
            let mut pool = player.library.iter().copied().collect::<Vec<_>>();
            let mut ordered = Vec::with_capacity(pool.len());
            for name in previous {
                if let Some(index) = pool
                    .iter()
                    .position(|id| self.state.objects[id].name == *name)
                {
                    ordered.push(pool.remove(index));
                }
            }
            ordered.extend(pool);
            self.state.players[seat as usize].library = ordered.into_iter().collect();
        }
    }
}

impl PendingSearch {
    /// Distinct listed cards, exactly as many as can be found unless the
    /// search permits finding fewer.
    fn admits(&self, selected: &[ObjectId]) -> bool {
        let mut distinct = selected.to_vec();
        distinct.sort_by_key(|id| id.0);
        distinct.dedup();
        distinct.len() == selected.len()
            && selected.iter().all(|id| self.cards.contains(id))
            && if self.allows_partial_find {
                selected.len() <= self.count
            } else {
                selected.len() == self.count.min(self.cards.len())
            }
    }
}

fn select_by_name(
    groups: &[(String, Vec<ObjectId>)],
    size: usize,
    chosen: &mut Vec<ObjectId>,
    result: &mut Vec<GameAction>,
) {
    if size == 0 {
        result.push(GameAction::SelectCards {
            cards: chosen.clone(),
        });
        return;
    }
    let Some(((_, ids), rest)) = groups.split_first() else {
        return;
    };
    for take in (0..=size.min(ids.len())).rev() {
        chosen.extend(&ids[..take]);
        select_by_name(rest, size - take, chosen, result);
        chosen.truncate(chosen.len() - take);
    }
}

fn initial_order(state: &GameState, seat: usize) -> Vec<ObjectId> {
    state.players[seat]
        .hand
        .iter()
        .chain(state.players[seat].library.iter())
        .copied()
        .collect()
}

fn name_counts(names: &[String]) -> BTreeMap<&str, usize> {
    let mut counts = BTreeMap::new();
    for name in names {
        *counts.entry(name.as_str()).or_insert(0) += 1;
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;
    use phase_engine::types::actions::MulliganChoice;

    #[test]
    fn combat_damage_projection_accepts_only_actual_combat_hits_on_players() {
        let damage = |target, amount, is_combat| GameEvent::DamageDealt {
            source_id: ObjectId(7),
            target,
            amount,
            is_combat,
            excess: 0,
            source_incarnation: None,
        };
        assert_eq!(
            combat_damage_to_player(&damage(TargetRef::Player(PlayerId(1)), 3, true)),
            Some((1, 3))
        );
        assert_eq!(
            combat_damage_to_player(&damage(TargetRef::Player(PlayerId(0)), 0, true)),
            Some((0, 0))
        );
        for event in [
            damage(TargetRef::Player(PlayerId(1)), 3, false),
            damage(TargetRef::Object(ObjectId(8)), 3, true),
            GameEvent::DamagePrevented {
                source_id: ObjectId(7),
                target: TargetRef::Player(PlayerId(1)),
                amount: 3,
            },
            GameEvent::LifeChanged {
                player_id: PlayerId(1),
                amount: -3,
                new_total: Default::default(),
            },
            GameEvent::CombatDamageDealtToPlayer {
                player_id: PlayerId(1),
                source_amounts: vec![(ObjectId(7), 3)],
                total_damage: 3,
                source_incarnations: Vec::new(),
            },
        ] {
            assert_eq!(combat_damage_to_player(&event), None, "{event:?}");
        }
    }

    #[test]
    fn combat_damage_projection_counts_each_hit_without_counting_its_aggregate() {
        let events = [
            GameEvent::DamageDealt {
                source_id: ObjectId(7),
                target: TargetRef::Player(PlayerId(1)),
                amount: 3,
                is_combat: true,
                excess: 0,
                source_incarnation: None,
            },
            GameEvent::CombatDamageDealtToPlayer {
                player_id: PlayerId(1),
                source_amounts: vec![(ObjectId(7), 3)],
                total_damage: 3,
                source_incarnations: Vec::new(),
            },
            GameEvent::DamageDealt {
                source_id: ObjectId(7),
                target: TargetRef::Player(PlayerId(1)),
                amount: 3,
                is_combat: true,
                excess: 0,
                source_incarnation: None,
            },
            GameEvent::CombatDamageDealtToPlayer {
                player_id: PlayerId(1),
                source_amounts: vec![(ObjectId(7), 3)],
                total_damage: 3,
                source_incarnations: Vec::new(),
            },
        ];
        let hits = events
            .iter()
            .filter_map(combat_damage_to_player)
            .collect::<Vec<_>>();
        assert_eq!(hits, vec![(1, 3), (1, 3)]);
        assert_eq!(hits.iter().map(|(_, amount)| amount).sum::<u32>(), 6);
    }

    fn runtime() -> PhaseRuntime {
        let land = |name, oracle_id| {
            serde_json::json!({
                "name": name,
                "mana_cost": {"type": "NoCost"},
                "card_type": {"supertypes": ["Basic"], "core_types": ["Land"], "subtypes": ["Mountain"]},
                "power": null, "toughness": null, "loyalty": null, "defense": null,
                "oracle_text": "", "non_ability_text": null, "flavor_name": null,
                "keywords": [], "abilities": [], "triggers": [], "static_abilities": [],
                "replacements": [], "color_override": null, "color_identity": ["Red"],
                "scryfall_oracle_id": oracle_id, "legalities": {}, "printings": ["TST"]
            })
        };
        PhaseRuntime::from_card_data_json(
            &serde_json::json!({
                "alpha": land("Alpha", "00000000-0000-0000-0000-000000000001"),
                "beta": land("Beta", "00000000-0000-0000-0000-000000000002")
            })
            .to_string(),
        )
        .unwrap()
    }

    fn deck() -> Vec<String> {
        [vec!["Alpha".to_owned(); 20], vec!["Beta".to_owned(); 20]].concat()
    }

    #[test]
    fn simultaneous_mulligans_offer_each_seat_only_its_own_decision() {
        let (mut game, _) = runtime().new_limited_game([deck(), deck()], 7).unwrap();
        while !matches!(
            game.state().waiting_for,
            phase_engine::types::game_state::WaitingFor::MulliganDecision { .. }
        ) {
            let seat = game.pending_seats()[0];
            let action = game.legal_actions(seat).0[0].clone();
            game.submit(seat, action).unwrap();
        }
        let decision = |choice| GameAction::MulliganDecision { choice };
        let declare = vec![
            decision(MulliganChoice::Keep),
            decision(MulliganChoice::Mulligan),
        ];
        assert_eq!(game.legal_actions(0).0, declare);
        assert_eq!(game.legal_actions(1).0, declare);

        game.submit(0, decision(MulliganChoice::Mulligan)).unwrap();
        game.submit(0, decision(MulliganChoice::Keep)).unwrap();

        // Seat 0 now owes a card to the bottom of its library while seat 1 has
        // not yet decided.
        let bottoming = game.legal_actions(0).0;
        assert_eq!(bottoming.len(), 7);
        assert!(bottoming
            .iter()
            .all(|action| matches!(action, GameAction::SelectCards { .. })));
        assert_eq!(game.legal_actions(1).0, declare);
        for seat in [0, 1] {
            for action in game.legal_actions(seat).0 {
                let mut copy = PhaseGame {
                    state: game.state.clone(),
                    cards: game.cards.clone(),
                };
                copy.submit(seat, action.clone())
                    .unwrap_or_else(|error| panic!("seat {seat} offered {action:?}: {error}"));
            }
        }
    }

    #[test]
    fn fitting_setup_preserves_starter_order_and_opening_events() {
        let runtime = runtime();
        let ordered = [
            vec!["Beta".to_owned(); 7],
            vec!["Alpha".to_owned(); 20],
            vec!["Beta".to_owned(); 13],
        ]
        .concat();
        for starter in [0, 1] {
            for seed in [41, 42] {
                let (mut game, initial) = runtime
                    .new_limited_game_for_fitting(
                        [deck(), deck()],
                        [ordered.clone(), ordered.clone()],
                        starter,
                        seed,
                    )
                    .unwrap();
                assert_eq!(game.state().active_player.0, starter);
                assert_eq!(game.state().current_starting_player.0, starter);
                assert_eq!(game.state().seat_order[0].0, starter);
                assert!(initial.events.iter().any(|event| matches!(event,
                    GameEvent::TurnStarted { player_id, turn_number: 1 } if player_id.0 == starter
                )));
                assert!(!initial
                    .events
                    .iter()
                    .any(|event| matches!(event, GameEvent::StartingPlayerContest { .. })));
                for seat in 0..2 {
                    let state = game.state();
                    let names = initial_order(state, seat)
                        .iter()
                        .map(|id| state.objects[id].name.clone())
                        .collect::<Vec<_>>();
                    assert_eq!(names, ordered);
                    assert_eq!(state.players[seat].hand.len(), 7);
                    assert!(state.players[seat]
                        .hand
                        .iter()
                        .all(|id| state.objects[id].zone == Zone::Hand));
                    assert!(state.players[seat]
                        .library
                        .iter()
                        .all(|id| state.objects[id].zone == Zone::Library));
                }
                let opening_moves = initial
                    .events
                    .iter()
                    .filter(|event| {
                        matches!(
                            event,
                            GameEvent::ZoneChanged {
                                from: Some(Zone::Library),
                                to: Zone::Hand,
                                ..
                            }
                        )
                    })
                    .count();
                assert_eq!(opening_moves, 14);
                for event in &initial.events {
                    if let GameEvent::CardDrawn {
                        player_id,
                        object_id,
                        ..
                    } = event
                    {
                        assert!(game.state().players[usize::from(player_id.0)]
                            .hand
                            .contains(object_id));
                    }
                    if let GameEvent::ZoneChanged {
                        object_id,
                        from: Some(Zone::Library),
                        to: Zone::Hand,
                        ..
                    } = event
                    {
                        assert_eq!(game.state().objects[object_id].name, "Beta");
                        assert!(game
                            .state()
                            .players
                            .iter()
                            .any(|player| player.hand.contains(object_id)));
                    }
                }
                let before = game.fork();
                for seat in [0, 1] {
                    let keep = game
                        .legal_actions(seat)
                        .0
                        .into_iter()
                        .find(|action| {
                            matches!(
                                action,
                                GameAction::MulliganDecision {
                                    choice: MulliganChoice::Keep
                                }
                            )
                        })
                        .unwrap();
                    game.submit(seat, keep).unwrap();
                }
                assert_eq!(game.state().active_player.0, starter);
                assert!(matches!(
                    before.state().waiting_for,
                    WaitingFor::MulliganDecision { .. }
                ));
                assert!(!matches!(
                    game.state().waiting_for,
                    WaitingFor::MulliganDecision { .. }
                ));
            }
        }
    }

    #[test]
    fn search_selections_cover_each_name_multiset_once() {
        let groups = vec![
            (
                "Plains".to_owned(),
                vec![ObjectId(54), ObjectId(49), ObjectId(67)],
            ),
            ("Island".to_owned(), vec![ObjectId(81)]),
        ];
        let selections = |size| {
            let mut result = vec![];
            select_by_name(&groups, size, &mut vec![], &mut result);
            result
                .into_iter()
                .map(|action| match action {
                    GameAction::SelectCards { cards } => {
                        cards.into_iter().map(|id| id.0).collect::<Vec<_>>()
                    }
                    other => panic!("unexpected {other:?}"),
                })
                .collect::<Vec<_>>()
        };
        assert_eq!(selections(0), vec![Vec::<u64>::new()]);
        // The representative Phase offers is the first listed card; the Island
        // listed last is a separate answer, and the other Plains are not.
        assert_eq!(selections(1), vec![vec![54], vec![81]]);
        assert_eq!(selections(2), vec![vec![54, 49], vec![54, 81]]);
        assert_eq!(selections(4), vec![vec![54, 49, 67, 81]]);
        assert!(selections(5).is_empty());
    }

    #[test]
    fn search_answers_must_be_distinct_listed_cards_of_an_admitted_size() {
        let search = |allows_partial_find| PendingSearch {
            cards: vec![ObjectId(54), ObjectId(49), ObjectId(81)],
            count: 1,
            allows_partial_find,
        };
        let ids = |ids: &[u8]| {
            ids.iter()
                .map(|id| ObjectId((*id).into()))
                .collect::<Vec<_>>()
        };
        for partial in [true, false] {
            assert!(search(partial).admits(&ids(&[81])));
            assert!(search(partial).admits(&ids(&[54])));
            assert!(!search(partial).admits(&ids(&[7])), "unlisted card");
            assert!(!search(partial).admits(&ids(&[54, 81])), "too many");
        }
        assert!(search(true).admits(&[]));
        assert!(!search(false).admits(&[]));
        let mut two = search(false);
        two.count = 2;
        assert!(!two.admits(&ids(&[54, 54])), "repeated card");
        assert!(two.admits(&ids(&[54, 81])));
        // A search for more than remain must find all that remain.
        two.count = 5;
        assert!(two.admits(&ids(&[54, 49, 81])));
        assert!(!two.admits(&ids(&[54, 81])));
    }

    #[test]
    fn fitting_submission_outside_a_search_stays_limited_to_offered_actions() {
        let runtime = runtime();
        let (mut game, _) = runtime
            .new_limited_game_for_fitting([deck(), deck()], [deck(), deck()], 0, 41)
            .unwrap();
        assert!(game.search_choice_selections(0).is_empty());
        let card = game.state().players[0].library[0];
        let error = game
            .submit_for_fitting(0, GameAction::SelectCards { cards: vec![card] })
            .unwrap_err();
        assert!(matches!(error, BridgeError::Action(_)), "{error:?}");
        let keep = GameAction::MulliganDecision {
            choice: MulliganChoice::Keep,
        };
        assert!(game.submit_for_fitting(0, keep).is_ok());
    }

    #[test]
    fn shuffles_keep_the_reconstructed_name_order_of_the_remaining_library() {
        let runtime = runtime();
        let ordered = [
            vec!["Beta".to_owned(); 7],
            vec!["Alpha".to_owned(), "Beta".to_owned(), "Alpha".to_owned()],
            vec!["Beta".to_owned(); 12],
            vec!["Alpha".to_owned(); 18],
        ]
        .concat();
        let (mut game, _) = runtime
            .new_limited_game_for_fitting([deck(), deck()], [ordered.clone(), deck()], 0, 41)
            .unwrap();
        let before = game.library_names();
        assert_eq!(before[0], ordered[7..]);
        // A search took the top Alpha, then Phase shuffled the rest.
        let library = &mut game.state.players[0].library;
        let mut rest = library.iter().copied().skip(1).collect::<Vec<_>>();
        rest.reverse();
        *library = rest.into_iter().collect();
        let untouched = game.state.players[1].library.clone();
        let shuffle = |player: u8| {
            serde_json::from_value::<GameEvent>(serde_json::json!({
                "type": "PlayerPerformedAction",
                "data": {"player_id": player, "action": "ShuffledLibrary"}
            }))
            .unwrap()
        };
        let shuffled = game.state.players[0].library.clone();
        game.keep_order_through_shuffles(&before, &[]);
        game.keep_order_through_shuffles(&before, &[shuffle(1)]);
        assert_eq!(
            game.state.players[0].library, shuffled,
            "seat 0 was not shuffled"
        );
        game.keep_order_through_shuffles(&before, &[shuffle(0)]);
        let names = game.library_names();
        // The draw prefix survives; the removed Alpha is the deepest one.
        let expected = [
            vec!["Alpha".to_owned(), "Beta".to_owned(), "Alpha".to_owned()],
            vec!["Beta".to_owned(); 12],
            vec!["Alpha".to_owned(); 17],
        ]
        .concat();
        assert_eq!(names[0], expected);
        assert_eq!(game.state.players[1].library, untouched);
    }

    #[test]
    fn fitting_identity_ignores_only_the_cancelled_cast_guard() {
        let runtime = runtime();
        let (game, _) = runtime
            .new_limited_game_for_fitting([deck(), deck()], [deck(), deck()], 0, 41)
            .unwrap();
        assert_eq!(
            game.fitting_identity_json().unwrap(),
            game.checkpoint_json().unwrap()
        );
        let mut cancelled = game.fork();
        let card = cancelled.state.players[0].hand[0];
        cancelled.state.cancelled_casts.push(card);
        cancelled.state.cancelled_casts.push(card);
        assert_ne!(
            cancelled.checkpoint_json().unwrap(),
            game.checkpoint_json().unwrap()
        );
        assert_eq!(
            cancelled.fitting_identity_json().unwrap(),
            game.fitting_identity_json().unwrap()
        );
        assert_eq!(cancelled.state.cancelled_casts, vec![card, card]);
        let mut changed = cancelled.fork();
        changed.state.players[1].life -= 1;
        assert_ne!(
            changed.fitting_identity_json().unwrap(),
            game.fitting_identity_json().unwrap()
        );
    }

    #[test]
    fn fitting_setup_rejects_incomplete_or_different_multisets_and_invalid_starter() {
        let runtime = runtime();
        for invalid in [vec!["Alpha".to_owned(); 40], vec!["Alpha".to_owned(); 39]] {
            assert!(matches!(
                runtime.new_limited_game_for_fitting([deck(), deck()], [invalid, deck()], 0, 41),
                Err(BridgeError::FittingSetup(_))
            ));
        }
        assert!(matches!(
            runtime.new_limited_game_for_fitting([deck(), deck()], [deck(), deck()], 2, 41),
            Err(BridgeError::FittingSetup(_))
        ));
    }
}
