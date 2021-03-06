"""Upload files to Archive.org.

usage:
    ia upload <identifier> <file>... [options]...
    ia upload <identifier> - --remote-name=<name> [options]...
    ia upload <identifier> <file> --remote-name=<name> [options]...
    ia upload --spreadsheet=<metadata.csv> [options]...
    ia upload <identifier> --status-check
    ia upload --help

options:
    -h, --help
    -q, --quiet                       Turn off ia's output [default: False].
    -d, --debug                       Print S3 request parameters to stdout and exit
                                      without sending request.
    -r, --remote-name=<name>          When uploading data from stdin, this option sets the
                                      remote filename.
    -S, --spreadsheet=<metadata.csv>  bulk uploading.
    -m, --metadata=<key:value>...     Metadata to add to your item.
    -H, --header=<key:value>...       S3 HTTP headers to send with your request.
    -c, --checksum                    Skip based on checksum. [default: False]
    -n, --no-derive                   Do not derive uploaded files.
    --size-hint=<size>                Specify a size-hint for your item.
    --delete                          Delete files after verifying checksums
                                      [default: False].
    -R, --retries=<i>                 Number of times to retry request if S3 retruns a
                                      503 SlowDown error.
    -s, --sleep=<i>                   The amount of time to sleep between retries
                                      [default: 30].
    -l, --log                         Log upload results to file.
    --status-check                    Check if S3 is accepting requests to the given item.
"""
from __future__ import absolute_import, unicode_literals, print_function
import sys
import os
from tempfile import TemporaryFile
import csv

from docopt import docopt, printable_usage
from requests.exceptions import HTTPError
from schema import Schema, Use, Or, And, SchemaError
import six

from internetarchive.session import ArchiveSession
from internetarchive.cli.argparser import get_args_dict, get_xml_text
from internetarchive.utils import validate_ia_identifier


def _upload_files(item, files, upload_kwargs, prev_identifier=None, archive_session=None,
                  responses=None):
    """Helper function for calling :meth:`Item.upload`"""
    responses = [] if not responses else responses
    if (upload_kwargs['verbose']) and (prev_identifier != item.identifier):
        print('{0}:'.format(item.identifier))

    try:
        response = item.upload(files, **upload_kwargs)
        responses += response
    except HTTPError as exc:
        responses += [exc.response]
    finally:
        # Debug mode.
        if upload_kwargs['debug']:
            for i, r in enumerate(responses):
                if i != 0:
                    print('---')
                headers = '\n'.join(
                    [' {0}:{1}'.format(k, v) for (k, v) in r.headers.items()]
                )
                print('Endpoint:\n {0}\n'.format(r.url))
                print('HTTP Headers:\n{0}'.format(headers))
                return

        # Format error message for any non 200 responses that
        # we haven't caught yet,and write to stderr.
        if responses and responses[-1] and responses[-1].status_code != 200:
            filename = responses[-1].request.url.split('/')[-1]
            msg = get_xml_text(responses[-1].content)
            print(' * error uploading '
                  '{0} ({1}): {2}'.format(filename, responses[-1].status_code, msg),
                  file=sys.stderr)

    return responses


def main(argv, session):
    args = docopt(__doc__, argv=argv)

    # Validate args.
    s = Schema({
        six.text_type: Use(bool),
        '<identifier>': Or(None, And(str, validate_ia_identifier,
            error=('<identifier> should be between 3 and 80 characters in length, and '
                   'can only contain alphanumeric characters, underscores ( _ ), or '
                   'dashes ( - )'))),
        '<file>': And(
            And(lambda f: all(os.path.exists(x) for x in f if x != '-'),
                error='<file> should be a readable file or directory.'),
            And(lambda f: False if f == ['-'] and not args['--remote-name'] else True,
                error='--remote-name must be provided when uploading from stdin.')),
        '--remote-name': Or(None, And(str)),
        '--spreadsheet': Or(None, os.path.isfile,
            error='--spreadsheet should be a readable file.'),
        '--metadata': Or(None, And(Use(get_args_dict), dict),
            error='--metadata must be formatted as --metadata="key:value"'),
        '--header': Or(None, And(Use(get_args_dict), dict),
            error='--header must be formatted as --header="key:value"'),
        '--retries': Use(lambda x: int(x[0]) if x else 0),
        '--sleep': Use(lambda l: int(l[0]), error='--sleep value must be an integer.'),
        '--size-hint': Or(Use(lambda l: int(l[0]) if l else None), int, None,
            error='--size-hint value must be an integer.'),
        '--status-check': bool,
    })
    try:
        args = s.validate(args)
    except SchemaError as exc:
        print('{0}\n{1}'.format(str(exc), printable_usage(__doc__)), file=sys.stderr)
        sys.exit(1)

    # Status check.
    if args['--status-check']:
        if session.s3_is_overloaded():
            print('warning: {0} is over limit, and not accepting requests. '
                  'Expect 503 SlowDown errors.'.format(args['<identifier>']),
                  file=sys.stderr)
            sys.exit(1)
        else:
            print('success: {0} is accepting requests.'.format(args['<identifier>']))
            sys.exit()

    elif args['<identifier>']:
        item = session.get_item(args['<identifier>'])

    # Upload keyword arguments.
    if args['--size-hint']:
        args['--header']['x-archive-size-hint'] = args['--size-hint']

    queue_derive = True if args['--no-derive'] is False else False
    verbose = True if args['--quiet'] is False else False

    upload_kwargs = dict(
        metadata=args['--metadata'],
        headers=args['--header'],
        debug=args['--debug'],
        queue_derive=queue_derive,
        checksum=args['--checksum'],
        verbose=verbose,
        retries=args['--retries'],
        retries_sleep=args['--sleep'],
        delete=args['--delete'],
    )

    # Upload files.
    if not args['--spreadsheet']:
        if args['-']:
            local_file = TemporaryFile()
            local_file.write(sys.stdin.read())
            local_file.seek(0)
        else:
            local_file = args['<file>']

        if isinstance(local_file, (list, tuple, set)) and args['--remote-name']:
            local_file = local_file[0]
        if args['--remote-name']:
            files = {args['--remote-name']: local_file}
        else:
            files = local_file

        responses = _upload_files(item, files, upload_kwargs)

    # Bulk upload using spreadsheet.
    else:
        # Use the same session for each upload request.
        session = ArchiveSession()
        spreadsheet = csv.DictReader(open(args['--spreadsheet'], 'rU'))
        prev_identifier = None
        responses = []
        for row in spreadsheet:
            local_file = row['file']
            identifier = row['identifier']
            del row['file']
            del row['identifier']
            if (not identifier) and (prev_identifier):
                identifier = prev_identifier
            item = session.get_item(identifier)
            # TODO: Clean up how indexed metadata items are coerced
            # into metadata.
            md_args = ['{0}:{1}'.format(k.lower(), v) for (k, v) in row.items() if v]
            metadata = get_args_dict(md_args)
            upload_kwargs['metadata'].update(metadata)
            r = _upload_files(item, local_file, upload_kwargs, prev_identifier, session,
                              responses)
            responses += r
            prev_identifier = identifier

    if responses and not all(r and r.ok for r in responses):
        sys.exit(1)
