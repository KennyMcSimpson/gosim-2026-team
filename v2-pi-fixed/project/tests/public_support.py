"""Optional public-card checks; no private inputs or truth files are loaded."""
import csv
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

EXPERIMENT = Path(__file__).resolve().parents[2]
REPO = EXPERIMENT.parent
CARDS = Path(os.environ.get('GOSIM_PUBLIC_CARDS_DIR',
                            str(REPO / 'formal-recon-20261005/official/cards')))
RUNNER = Path(os.environ.get('GOSIM_OFFICIAL_RUNNER_DIR', str(
    REPO / 'training/official-examples/gosim-observer-examples/runner')))
if not RUNNER.is_dir():
    RUNNER = REPO / 'formal-recon-20261005/official/source/examples/_local/runner'


def public_input(card):
    directory = CARDS / card
    if not directory.is_dir() or not RUNNER.is_dir():
        raise unittest.SkipTest('Set GOSIM_PUBLIC_CARDS_DIR and GOSIM_OFFICIAL_RUNNER_DIR for public geometry checks')
    sys.path.insert(0, str(RUNNER))
    from challenge.v4_fiber_map import FiberGrid
    fiber = json.loads((directory / 'config/v4_fiber_config.json').read_text(encoding='utf-8'))
    score = json.loads((directory / 'config/v4_score_config.json').read_text(encoding='utf-8'))
    targets = list(csv.DictReader((directory / 'public/targets.csv').read_text(encoding='utf-8-sig').splitlines()))
    nights = list(csv.DictReader((directory / 'public/v4_night_calendar.csv').read_text(encoding='utf-8-sig').splitlines()))
    grid = FiberGrid.from_config(fiber)
    init = {'site': dict(fiber['site'], minimum_altitude_deg=30.0),
            'instrument': {'grid_side': grid.n_side, 'n_fibers': grid.n_fibers,
                           'glass_side_deg': round(grid.fiber_side_deg, 6), 'pitch_deg': round(grid.pitch_deg, 6),
                           'fov_side_deg': round(grid.fov_side_deg, 6), 'exposure': fiber['exposure']},
            'scoring': score, 'survey': {'start_utc': nights[0]['observing_start_utc'],
                                       'end_utc': nights[-1]['observing_end_utc'], 'nights': nights},
            'targets': {'columns': ['target_id', 'ra_deg', 'dec_deg', 'feature_flux', 'science_weight', 'required'],
                        'rows': [[r['target_id'], float(r['ra_deg']), float(r['dec_deg']), float(r['feature_flux']),
                                  float(r['science_weight']), r['required'].lower() == 'true'] for r in targets]}}
    return init, fiber, {r['target_id']: r for r in targets}


def check_action(envelope, now, fiber, targets):
    from challenge.v4_runner import normalize_action
    from challenge.v4_fiber_map import FiberGrid
    assert envelope['protocol_version'] == 'participant-agent-protocol-v4'
    assert envelope['message_type'] == 'decision_response'
    action = {k: v for k, v in envelope.items() if k not in
              {'protocol_version', 'message_type', 'decision_sequence', 'reason', 'decision_source'}}
    normal = normalize_action(action, SimpleNamespace(fiber_config=fiber), targets, now)
    if action['action'] == 'observe':
        grid = FiberGrid.from_config(fiber)
        for f, tid in normal['assignments'].items():
            row = targets[tid]
            hit = grid.classify_target(float(row['ra_deg']), float(row['dec_deg']), now,
                                      action['pointing']['alt_deg'], action['pointing']['az_deg'],
                                      fiber['site']['latitude_deg'], fiber['site']['longitude_deg'])
            assert hit.fiber_id == f and hit.region == 'glass'
        assert len(set(normal['assignments'].values())) == len(normal['assignments'])
