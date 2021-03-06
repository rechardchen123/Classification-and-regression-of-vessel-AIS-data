
"""

Example:


python compute_metrics.py     --inference-path classification_results.json.gz     \
                              --label-path classification/data/net_training_20161115.csv   \
                              --dest-path fltest.html --fishing-ranges classification/data/combined_fishing_ranges.csv  \
                              --dump-labels-to . \
                              --skip-localisation-metrics

"""
from __future__ import division
from __future__ import absolute_import
from __future__ import print_function
import os
import csv
import subprocess
import numpy as np
import pandas as pd
import dateutil.parser
import logging
import argparse
from collections import namedtuple, defaultdict
import sys
import yattag
import newlinejson as nlj
from classification.utility import VESSEL_CLASS_DETAILED_NAMES, VESSEL_CATEGORIES, TEST_SPLIT, schema, atomic
import gzip
import dateutil.parser
import datetime
import pytz
from .ydump import css, ydump_table



coarse_categories = [
    'cargo_or_tanker', 'passenger', 'seismic_vessel', 'tug', 'other_fishing', 
    'drifting_longlines', 'seiners', 'fixed_gear', 'squid_jigger', 'trawlers', 
    'other_not_fishing']

coarse_mapping = defaultdict(set)
for k0, extra in [('fishing', 'other_fishing'), 
                  ('non_fishing', 'other_not_fishing')]:
    for k1, v1 in schema['unknown'][k0].items():
        key = k1 if (k1 in coarse_categories) else extra
        if v1 is None:
            coarse_mapping[key] |= {k1}
        else:
            coarse_mapping[key] |= set(atomic(v1))

coarse_mapping = [(k, coarse_mapping[k]) for k in coarse_categories]

fishing_mapping = [
    ['fishing', set(atomic(schema['unknown']['fishing']))],
    ['non_fishing', set(atomic(schema['unknown']['non_fishing']))],
]


fishing_category_map = {}
atomic_fishing = fishing_mapping[0][1]
for coarse, fine in coarse_mapping:
    for atomic in fine:
        if atomic in atomic_fishing:
            fishing_category_map[atomic] = coarse

print(fishing_category_map )


# Faster than using dateutil
def _parse(x):
    if isinstance(x, datetime.datetime):
        return x
    # 2014-08-28T13:56:16+00:00
    # TODO: fix generation to generate consistent datetimes
    if x[-6:] == '+00:00':
        x = x[:-6]
    if x.endswith('.999999'):
        x = x[:-7]
    if x.endswith('Z'):
        x = x[:-1]
    try:
        dt = datetime.datetime.strptime(x, '%Y-%m-%dT%H:%M:%S')
    except:
        logging.fatal('Could not parse "%s"', x)
        raise
    return dt.replace(tzinfo=pytz.UTC)


LocalisationResults = namedtuple('LocalisationResults',
                                 ['true_fishing_by_mmsi',
                                  'pred_fishing_by_mmsi', 'label_map'])

FishingRange = namedtuple('FishingRange',
    ['is_fishing', 'start_time', 'end_time'])


def ydump_fishing_localisation(doc, results):
    doc, tag, text, line = doc.ttl()

    y_true = np.concatenate(results.true_fishing_by_mmsi.values())
    y_pred = np.concatenate(results.pred_fishing_by_mmsi.values())

    header = ['Gear Type (mmsi:true/total)', 'Precision', 'Recall', 'Accuracy', 'F1-Score']
    rows = []
    logging.info('Overall localisation accuracy %s',
                 accuracy_score(y_true, y_pred))
    logging.info('Overall localisation precision %s',
                 precision_score(y_true, y_pred))
    logging.info('Overall localisation recall %s',
                 recall_score(y_true, y_pred))

    for cls in sorted(set(fishing_category_map.values())) + ['other'] :
        true_chunks = []
        pred_chunks = []
        mmsi_list = []
        for mmsi in results.label_map:
            if mmsi not in results.true_fishing_by_mmsi:
                continue
            if fishing_category_map.get(results.label_map[mmsi], 'other') != cls:
                continue
            mmsi_list.append(mmsi)
            true_chunks.append(results.true_fishing_by_mmsi[mmsi])
            pred_chunks.append(results.pred_fishing_by_mmsi[mmsi])
        if len(true_chunks):
            logging.info('MMSI for {}: {}'.format(cls, mmsi_list))
            y_true = np.concatenate(true_chunks)
            y_pred = np.concatenate(pred_chunks)
            rows.append(['{} ({}:{}/{})'.format(cls, len(true_chunks), sum(y_true), len(y_true)),
                         precision_score(y_true, y_pred),
                         recall_score(y_true, y_pred),
                         accuracy_score(y_true, y_pred),
                         f1_score(y_true, y_pred), ])

    rows.append(['', '', '', '', ''])

    y_true = np.concatenate(results.true_fishing_by_mmsi.values())
    y_pred = np.concatenate(results.pred_fishing_by_mmsi.values())

    rows.append(['Overall',
                 precision_score(y_true, y_pred),
                 recall_score(y_true, y_pred),
                 accuracy_score(y_true, y_pred),
                 f1_score(y_true, y_pred), ])

    with tag('div', klass='unbreakable'):
        ydump_table(
            doc, header,
            [[('{:.2f}'.format(x) if isinstance(x, float) else x) for x in row]
             for row in rows])




def precision_score(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=bool)
    y_pred = np.asarray(y_pred, dtype=bool)

    true_pos = y_true & y_pred
    all_pos = y_pred

    return true_pos.sum() / all_pos.sum()


def recall_score(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=bool)
    y_pred = np.asarray(y_pred, dtype=bool)

    true_pos = y_true & y_pred
    all_true = y_true

    return true_pos.sum() / all_true.sum()


def f1_score(y_true, y_pred):
    prec = precision_score(y_true, y_pred)
    recall = recall_score(y_true, y_pred)

    return 2 / (1 / prec + 1 / recall)

def accuracy_score(y_true, y_pred, weights=None):
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    if weights is None:
        weights = np.ones_like(y_pred).astype(float)
    weights = np.asarray(weights)

    correct = (y_true == y_pred)

    return (weights * correct).sum() / weights.sum()


def load_inferred_fishing(table, id_list, project_id, threshold=True):
    """Load inferred data and generate comparison data

    """
    query_template = """
    SELECT vessel_id, start_time, end_time, nnet_score FROM 
        TABLE_DATE_RANGE([{table}],
            TIMESTAMP('{year}-01-01'), TIMESTAMP('{year}-12-31'))
        WHERE vessel_id in ({ids})
    """
    ids = ','.join('"{}"'.format(x) for x in id_list)
    ranges = defaultdict(list)
    for year in range(2012, 2018):
        query = query_template.format(table=table, year=year, ids=ids)
        print(query)
        for x in pd.read_gbq(query, project_id=project_id).itertuples():
            score = x.nnet_score
            if threshold:
                score = score > 0.5
            start = x.start_time.replace(tzinfo=pytz.utc)
            end = x.end_time.replace(tzinfo=pytz.utc)
            ranges[x.vessel_id].append(FishingRange(score, start, end))
    print([(key, len(val)) for (key, val) in ranges.items()])
    return ranges

def load_true_fishing_ranges_by_mmsi(fishing_range_path,
                                     split_map,
                                     threshold=True):
    ranges_by_mmsi = defaultdict(list)
    parse = dateutil.parser.parse
    with open(fishing_range_path) as f:
        for row in csv.DictReader(f):
            mmsi = row['mmsi'].strip()
            if not split_map.get(mmsi) == TEST_SPLIT:
                continue
            val = float(row['is_fishing'])
            if threshold:
                val = val > 0.5
            rng = (val, parse(row['start_time']), parse(row['end_time']))
            ranges_by_mmsi[mmsi].append(rng)
    return ranges_by_mmsi


def datetime_to_minute(dt):
    timestamp = (dt - datetime.datetime(
        1970, 1, 1, tzinfo=pytz.utc)).total_seconds()
    return int(timestamp // 60)


def compare_fishing_localisation(inferred_ranges, fishing_range_path,
                                 label_map, split_map):

    logging.debug('loading fishing ranges')
    true_ranges_by_mmsi = load_true_fishing_ranges_by_mmsi(fishing_range_path,
                                                           split_map)
    true_by_mmsi = {}
    pred_by_mmsi = {}

    for mmsi in sorted(true_ranges_by_mmsi.keys()):
        logging.debug('processing %s', mmsi)
        if str(mmsi) not in inferred_ranges:
            continue
        true_ranges = true_ranges_by_mmsi[mmsi]
        if not true_ranges:
            continue

        # Determine minutes from start to finish of this mmsi, create an array to
        # hold results and fill with -1 (unknown)
        logging.debug('processing %s true ranges', len(true_ranges))
        logging.debug('finding overall range')
        _, start, end = true_ranges[0]
        for (_, s, e) in true_ranges[1:]:
            start = min(start, s)
            end = max(end, e)
        start_min = datetime_to_minute(start)
        end_min = datetime_to_minute(end)
        minutes = np.empty([end_min - start_min + 1, 2], dtype=int)
        minutes.fill(-1)

        # Fill in minutes[:, 0] with known true / false values
        logging.debug('filling 0s')
        for (is_fishing, s, e) in true_ranges:
            s_min = datetime_to_minute(s)
            e_min = datetime_to_minute(e)
            for m in range(s_min - start_min, e_min - start_min + 1):
                minutes[m, 0] = is_fishing

        # fill in minutes[:, 1] with inferred true / false values
        logging.debug('filling 1s')
        for (is_fishing, s, e) in inferred_ranges[str(mmsi)]:
            s_min = datetime_to_minute(s)
            e_min = datetime_to_minute(e)
            for m in range(s_min - start_min, e_min - start_min + 1):
                if 0 <= m < len(minutes):
                    minutes[m, 1] = is_fishing

        mask = ((minutes[:, 0] != -1) & (minutes[:, 1] != -1))

        if mask.sum():
            accuracy = (
                (minutes[:, 0] == minutes[:, 1]) * mask).sum() / mask.sum()
            logging.debug('Accuracy for MMSI %s: %s', mmsi, accuracy)

            true_by_mmsi[mmsi] = minutes[mask, 0]
            pred_by_mmsi[mmsi] = minutes[mask, 1]

    return LocalisationResults(true_by_mmsi, pred_by_mmsi, label_map)


def compute_results(args):
    logging.info('Loading label maps')
    maps = defaultdict(dict)
    with open(args.label_path) as f:
        for row in csv.DictReader(f):
            mmsi = row['mmsi'].strip()
            if not row['split'] == TEST_SPLIT:
                continue
            for field in ['label', 'split']:
                if row[field]:
                    if field == 'label':
                        if row[field].strip(
                        ) not in VESSEL_CLASS_DETAILED_NAMES:
                            continue
                    maps[field][mmsi] = row[field]

    # Sanity check the attribute mappings
    for field in ['length', 'tonnage', 'engine_power', 'crew_size']:
        for mmsi, value in maps[field].items():
            assert float(value) > 0, (mmsi, value)

    logging.info('Loading inference data')
    vessel_ids = set([x for x in maps['split'] if maps['split'][x] == TEST_SPLIT]) \

    fishing_ranges = load_inferred_fishing(args.inference_table, vessel_ids, args.project_id)

    logging.info('Comparing localisation')
    results = {}
    results['localisation'] = compare_fishing_localisation(
        fishing_ranges, args.fishing_ranges, maps['label'],
        maps['split'])


    return results


def dump_html(args, results):

    doc = yattag.Doc()

    with doc.tag('style', type='text/css'):
        doc.asis(css)

    logging.info('Dumping Localisation')
    doc.line('h2', 'Fishing Localisation')
    ydump_fishing_localisation(doc, results['localisation'])
    doc.stag('hr')

    with open(args.dest_path, 'w') as f:
        logging.info('Writing output')
        f.write(yattag.indent(doc.getvalue(), indent_text=True))


"""

python -m classification.metrics.compute_fishing_metrics \
--inference-table world-fishing-827:machine_learning_dev_ttl_30d.test_dataflow_2016_ \
--label-path classification/data/fishing_classes.csv \
--dest-path test_fishing.html \
--fishing-ranges classification/data/combined_fishing_ranges.csv \
 --project-id world-fishing-827


"""


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.DEBUG)

    parser = argparse.ArgumentParser(
        description='Test fishing inference results and output metrics.\n')
    parser.add_argument(
        '--inference-table', help='table of inference results', required=True)
    parser.add_argument(
        '--project-id', help='Google Cloud project id', required=True)
    parser.add_argument(
        '--label-path', help='path to test data', required=True)
    parser.add_argument('--fishing-ranges', help='path to fishing range data')
    parser.add_argument(
        '--dest-path', help='path to write results to', required=True)

    parser.add_argument('--test-only', action='store_true')

    args = parser.parse_args()

    results = compute_results(args)

    dump_html(args, results)

 