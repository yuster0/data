#!/usr/bin/env python
import collections
import csv
import datetime
import logging
import os
import re
import requests
import tempfile
import time
import zipfile

import openpyxl

import dataclass
import mappings


KOOFR_ROOT = 'https://app.koofr.net/'

logging.basicConfig(level=logging.INFO)

passwd = os.getenv('ZD_ZIP_PASS')
assert passwd, 'Environmental variable ZD_ZIP_PASS must be set.'


logging.info('Refreshing Koofr cache...')
url = f'{KOOFR_ROOT}api/v2/public/links/b232782b-9893-4278-b54c-faf461fce4bd/bundle?path=%2F&password={passwd}'
requests.get(url, headers={'User-Agent': 'curl'})
time.sleep(5)

with tempfile.TemporaryDirectory() as tmp_dir:
    zip_path = os.path.join(tmp_dir, 'zd.zip')
    url = f'{KOOFR_ROOT}content/links/b232782b-9893-4278-b54c-faf461fce4bd/files/get/ZD.zip?path=%2F&password={passwd}'

    logging.info('Downloading archive...')
    resp = requests.get(url, headers={'User-Agent': 'curl'})
    with open(zip_path, 'wb') as f:
        f.write(resp.content)

    logging.info('Extracting archive...')
    file = zipfile.ZipFile(zip_path)
    file.extractall(path=tmp_dir)
    os.remove(zip_path)

    logging.info('Validating archive and fetching sheets...')
    list_dir = [os.path.join(tmp_dir, x) for x in os.listdir(tmp_dir)]
    files = [x for x in list_dir if os.path.isfile(x)]
    assert not [x for x in list_dir if os.path.isdir(x)], f'Archive should contain only files, no folders'
    assert all(f.endswith('.xlsx') for f in files), 'All files should end with .xlsx'
    sheets = []
    for f in files:
        sheet = openpyxl.load_workbook(f).active
        sheet.file = f
        sheets.append(sheet)

logging.info('Validating columns...')
# file content validation
for sheet in sheets:
    expected = [  # header row 1
        'Št.', 'ZD', 'Datum', 'Št. pregledov NMP', 'Št. pregledov  suma na COVID',
        r'Št. sumov na COVID brez pregleda \(triaža po telefonu\)', 'Št. opravljenih testiranj COVID',
        'Št.  pozitivnih COVID', 'Št. napotitev v bolnišnico', 'Št. napotitev v samoosamitev', 'Opombe'
    ]
    actual = [cell.value for cell in sheet[1]]
    for expected_col, actual_col in zip(expected, actual):
        assert re.match(expected_col, actual_col), (sheet, expected_col, actual_col)

logging.info('Reading xlsx files and building entity collection...')
entities = []
for sheet in sheets:
    for row in list(sheet.iter_rows())[2:]:  # skip header rows
        if row[0].value is None and row[1].value == 'SKUPAJ':
            break  # aggregates do not interest us, also do not continue since all the relevant data is extracted
        # make sure the name is always there
        assert row[1].value is not None, sheet
        # make sure the date is always there
        if row[2].value is None:  # some cells are missing dates
            search = re.search(r'<Worksheet "(\d{1,2})\.(\d{1,2})\.">', str(sheet))
            assert datetime.datetime.now().year == 2020  # this extraction logic could be broken in 2021
            date = datetime.datetime(year=2020, month=int(search.group(2)), day=int(search.group(1))).date()
        else:
            date = row[2].value.date()
        entities.append(dataclass.Entity(
            name=row[1].value,
            # the date we get from xslx means date when data was gathered, the data itself is for the day before that
            date=date - datetime.timedelta(days=1),
            sheet=str(sheet),
            file=sheet.file,
            numbers=dataclass.Numbers(
                examinations___medical_emergency=row[3].value,  # Št. pregledov NMP
                examinations___suspected_covid=row[4].value,  # Št. pregledov  suma na COVID
                phone_triage___suspected_covid=row[5].value,  # Št. sumov na COVID brez pregleda (triaža po telefonu)
                tests___performed=row[6].value,  # Št. opravljenih testiranj COVID
                tests___positive=row[7].value,  # Št.  pozitivnih COVID
                sent_to___hospital=row[8].value,  # Št. napotitev v bolnišnico
                sent_to___self_isolation=row[9].value  # Št. napotitev v samoosamitev
            )
        ))

aggregates = collections.defaultdict(lambda: dataclass.Numbers(0, 0, 0, 0, 0, 0, 0))
for entity in entities:
    for key in dataclass.Numbers.__annotations__.keys():
        aggregates[entity.date].__dict__[key] += entity.numbers.__dict__[key] or 0  # handle Null

logging.info('Writing CSV...')
repo_root_health_centers = os.path.dirname(os.path.abspath(__file__))
assert repo_root_health_centers.endswith('/data/health_centers')
repo_root = '/'.join(repo_root_health_centers.split('/')[:-1])
assert repo_root.endswith('/data')
health_centers_csv = os.path.join(repo_root, 'csv/health_centers.csv')


def get_entity(name_key: str, date: datetime):
    found_entities = []
    for entity in entities:
        if entity.name_key == name_key and entity.date == date:
            found_entities.append(entity)
    assert len(found_entities), (name_key, date)  # no entities found
    if len(found_entities) > 1:
        for found_entity in found_entities:
            logging.error(found_entity)
        raise Exception(f'Too many entities found: {len(found_entities)}, {name_key}, {date}')
    return found_entities[0]


with open(health_centers_csv, 'w', newline='') as csvfile:
    writer = csv.writer(csvfile, dialect='excel')

    def get_formatted_numbers_fields():
        return[field.replace('___', '.') for field in dataclass.Numbers.__annotations__.keys()]

    columns = ['date']
    # scope: aggregates
    for field in get_formatted_numbers_fields():
        columns.append(f'hc.{field}')
    # scope: health centers
    for name in mappings.unique_short_names:
        region = mappings.region[name]
        for field in get_formatted_numbers_fields():
            columns.append(f'hc.{region}.{name}.{field}')
    writer.writerow(columns)

    # write data
    dates = sorted(list(set([entity.date for entity in entities])))
    for date in dates:

        columns = [date]
        # scope: aggregates
        for key in aggregates[date].__annotations__.keys():
            columns.append(aggregates[date].__dict__[key])
        # scope: health centers
        for name in mappings.unique_short_names:
            entity = get_entity(name_key=name, date=date)
            for field in dataclass.Numbers.__annotations__.keys():
                columns.append(getattr(entity.numbers, field))
        writer.writerow(columns)

logging.info('Writing CSV timestamp...')
with open(f'{health_centers_csv}.timestamp', 'w') as timestamp_file:
    timestamp = int(time.time())
    timestamp_file.write(f'{timestamp}\n')
