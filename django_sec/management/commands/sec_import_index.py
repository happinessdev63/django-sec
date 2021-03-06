from __future__ import print_function

import os
import sys
from zipfile import ZipFile
from datetime import date, datetime, timedelta
from optparse import make_option

import django
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from django.conf import settings
from django.utils import timezone
from django.utils.encoding import force_text
from django.db.transaction import TransactionManagementError

import six
from six.moves.urllib.request import urlopen

from django_sec.models import Company, Index, IndexFile, DATA_DIR

def removeNonAscii(s):
    return "".join(i for i in s if ord(i) < 128)

def get_options(parser=None):
    make_opt = make_option
    if parser:
        make_opt = parser.add_argument
    return [
        make_opt('--start-year',
            default=None),
        make_opt('--end-year',
            default=None),
        make_opt('--quarter',
            default=None),
        make_opt('--delete-prior-indexes',
            action='store_true',
            default=False),
        make_opt('--reprocess',
            action='store_true',
            default=False),
        make_opt('--auto-reprocess-last-n-days',
            default=90,
            help='The number of days to automatically redownload and reprocess index files.'),
        make_opt('--max-lines',
            default=0),
        make_opt('--dryrun',
            action='store_true',
            default=False),
    ]

class Command(BaseCommand):
    help = "Download new files representing one month of 990s, ignoring months we already have. "\
        "Each quarter contains hundreds of thousands of filings; will take a while to run. "
    #args = ''
    option_list = getattr(BaseCommand, 'option_list', ()) + tuple(get_options())
        
    def create_parser(self, prog_name, subcommand):
        """
        For ``Django>=1.10``
        Create and return the ``ArgumentParser`` which extends ``BaseCommand`` parser with
        chroniker extra args and will be used to parse the arguments to this command.
        """
        from distutils.version import StrictVersion # pylint: disable=E0611
        parser = super(Command, self).create_parser(prog_name, subcommand)
        version_threshold = StrictVersion('1.10')
        current_version = StrictVersion(django.get_version(django.VERSION))
        if current_version >= version_threshold:
            get_options(parser)
            self.add_arguments(parser)
        return parser
    
    def handle(self, **options):
        
        self.dryrun = options['dryrun']
        
        max_lines = int(options['max_lines'])
        
        start_year = options['start_year']
        if start_year:
            start_year = int(start_year)
        else:
            start_year = date.today().year - 1
            
        end_year = options['end_year']
        if end_year:
            end_year = int(end_year)
        else:
            end_year = date.today().year+1
        
        reprocess = options['reprocess']
        
        target_quarter = options['quarter']
        if target_quarter:
            target_quarter = int(target_quarter)
        
        auto_reprocess_last_n_days = int(options['auto_reprocess_last_n_days'])
        
        tmp_debug = settings.DEBUG
        settings.DEBUG = False
        try:
            for year in range(start_year, end_year):
                for quarter in range(4):
                    if target_quarter and quarter+1 != target_quarter:
                        continue
                    quarter_start = date(year, quarter*3+1, 1)
                    cutoff_date = date.today() - timedelta(days=auto_reprocess_last_n_days)
                    _reprocess = reprocess or (quarter_start > cutoff_date)
                    self.get_filing_list(
                        year,
                        quarter+1,
                        reprocess=_reprocess,
                        max_lines=max_lines)
        finally:
            settings.DEBUG = tmp_debug
            connection.close()
                
    def get_filing_list(self, year, quarter, reprocess=False, max_lines=None):
        """
        Gets the list of filings and download locations for the given year and quarter.
        """
        
        def commit():
            try:
                transaction.commit()
            except TransactionManagementError:
                pass

        url = 'https://www.sec.gov/Archives/edgar/full-index/%d/QTR%d/company.zip' % (year, quarter)
    
        # Download the data and save to a file
        if not os.path.isdir(DATA_DIR):
            os.makedirs(DATA_DIR)
        fn = os.path.join(DATA_DIR, 'company_%d_%d.zip' % (year, quarter))
    
        ifile, _ = IndexFile.objects.get_or_create(
            year=year, quarter=quarter, defaults=dict(filename=fn))
        if ifile.processed and not reprocess:
            return
        ifile.filename = fn
        
        if os.path.exists(fn) and reprocess:
            print('Deleting old file %s.' % fn)
            os.remove(fn)
        
        if self.dryrun:
            return
        
        if not os.path.exists(fn):
            print('Downloading %s.' % (url,))
            try:
                compressed_data = urlopen(url).read()
            except IOError as e:
                print('Unable to download url: %s' % e)
                return
            fileout = open(fn, 'wb')
            fileout.write(compressed_data)
            fileout.close()
            ifile.downloaded = timezone.now()
        
        if not ifile.downloaded:
            ifile.downloaded = timezone.now()
        ifile.save()
        commit()
        
        # Extract the compressed file
        print('Opening index file %s.' % (fn,))
        zipf = ZipFile(fn)
        zdata = zipf.read('company.idx')
        #zdata = removeNonAscii(zdata)
        
        # Parse the fixed-length fields
        bulk_companies = []
        bulk_indexes = []
        bulk_commit_freq = 1000
        status_secs = 3
        
        # In Python3, default type is now bytes, so we have to convert back to string.
        if not isinstance(zdata, six.string_types):
            zdata = zdata.decode()
            
        lines = zdata.split('\n')
        i = 0
        total = len(lines)
        IndexFile.objects.filter(id=ifile.id).update(total_rows=total)
        last_status = None
        prior_keys = set()
        #print('Found %i prior index keys.' % len(prior_keys)
        prior_ciks = set(Company.objects.all().values_list('cik', flat=True))
        print('Found %i prior ciks.' % len(prior_ciks))
        index_add_count = 0
        company_add_count = 0
        for r in lines[10:]: # Note, first 10 lines are useless headers.
            i += 1
            if not reprocess and ifile.processed_rows and i < ifile.processed_rows:
                continue
            if not last_status or ((datetime.now() - last_status).seconds >= status_secs):
                sys.stdout.write(
                    '\rProcessing record %i of %i (%.02f%%).' % (i, total, float(i)/total*100))
                sys.stdout.flush()
                last_status = datetime.now()
                IndexFile.objects.filter(id=ifile.id).update(processed_rows=i)
            dt = r[86:98].strip()
            if not dt:
                continue
            dt = date(*map(int, dt.split('-')))
            if r.strip() == '':
                continue
            name = r[0:62].strip()
            
            cik = int(r[74:86].strip())
            if cik not in prior_ciks:
                company_add_count += 1
                prior_ciks.add(cik)
                bulk_companies.append(Company(cik=cik, name=force_text(name, errors='replace')))
                
            filename = r[98:].strip()
            key = (cik, dt, filename)#, year, quarter)
            if key in prior_keys:
                continue
            prior_keys.add(key)
            if Index.objects.filter(company__cik=cik, date=dt, filename=filename).exists():
                continue
            index_add_count += 1
            bulk_indexes.append(Index(
                company_id=cik,
                form=r[62:74].strip(), # form type
                date=dt, # date filed
                year=year,
                quarter=quarter,
                filename=filename,
            ))
            if not len(bulk_indexes) % bulk_commit_freq:
                if len(bulk_companies):
                    Company.objects.bulk_create(bulk_companies)
                    bulk_companies = []
                Index.objects.bulk_create(bulk_indexes)
                bulk_indexes = []
                commit()
            
            # Mainly used during unittesting to limit processing.
            if max_lines and i >= max_lines:
                break
                
        if bulk_indexes:
            if len(bulk_companies):
                Company.objects.bulk_create(bulk_companies)
                bulk_companies = []
            Index.objects.bulk_create(bulk_indexes)
        IndexFile.objects.filter(id=ifile.id).update(processed=timezone.now())
        commit()
        
        print('\rProcessing record %i of %i (%.02f%%).' % (total, total, 100))
        print()
        print('%i new companies found.' % company_add_count)
        print('%i new indexes found.' % index_add_count)
        sys.stdout.flush()
        IndexFile.objects.filter(id=ifile.id).update(processed_rows=total)
