"""Tests for `pgsync` package."""
import mock
import psycopg2
import pytest

from pgsync.base import subtransactions
from pgsync.exc import (
    ColumnNotFoundError,
    NodeAttributeError,
    TableNotInNodeError,
)
from pgsync.sync import Sync

from .helpers.utils import assert_resync_empty, search, truncate_slots


@pytest.mark.usefixtures('table_creator')
class TestRoot(object):
    """Root only node tests."""

    @pytest.fixture(scope='function')
    def data(self, sync, book_cls, publisher_cls):
        session = sync.session

        books = [
            book_cls(
                isbn='abc',
                title='The Tiger Club',
                description='Tigers are fierce creatures',
                publisher=publisher_cls(name='Tiger publishing')
            ),
            book_cls(
                isbn='def',
                title='The Lion Club',
                description='Lion and the mouse',
                publisher=publisher_cls(name='Lion publishing')
            ),
            book_cls(
                isbn='ghi',
                title='The Rabbit Club',
                description='Rabbits on the run',
                publisher=publisher_cls(name='Hop Bunny publishing')
            )
        ]

        with subtransactions(session):
            conn = session.connection().engine.connect().connection
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
            )
            cursor = conn.cursor()
            channel = sync.database
            cursor.execute(f'UNLISTEN {channel}')

        with subtransactions(session):
            session.add_all(books)

        sync.logical_slot_get_changes(
            f'{sync.database}_testdb',
            upto_nchanges=None,
        )

        yield books

        with subtransactions(session):
            conn = session.connection().engine.connect().connection
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
            )
            cursor = conn.cursor()
            channel = session.connection().engine.url.database
            cursor.execute(f'UNLISTEN {channel}')

        with subtransactions(session):
            sync.truncate_tables(
                [
                    book_cls.__table__.name,
                    publisher_cls.__table__.name
                ]
            )

        sync.logical_slot_get_changes(
            f'{sync.database}_testdb',
            upto_nchanges=None,
        )

        try:
            sync.es.teardown(index='testdb')
        except Exception:
            raise

        sync.redis._delete()
        session.connection().engine.connect().close()
        session.connection().engine.dispose()

    def test_sync(self, sync, data):
        """Test the sync with a root only node."""
        node = {
            'table': 'book',
            'columns': ['isbn', 'title', 'description']
        }
        txmin = sync.checkpoint
        txmax = sync.txid_current
        docs = [doc for doc in sync._sync(node, txmin=txmin, txmax=txmax)]
        assert docs == [
            {
                '_id': 'abc',
                '_index': 'testdb',
                '_source': {
                    '_meta': {},
                    'description': 'Tigers are fierce creatures',
                    'isbn': 'abc',
                    'title': 'The Tiger Club'
                }
            },
            {
                '_id': 'def',
                '_index': 'testdb',
                '_source': {
                    '_meta': {},
                    'description': 'Lion and the mouse',
                    'isbn': 'def',
                    'title': 'The Lion Club'
                }
            },
            {
                '_id': 'ghi',
                '_index': 'testdb',
                '_source': {
                    '_meta': {},
                    'description': 'Rabbits on the run',
                    'isbn': 'ghi',
                    'title': 'The Rabbit Club'
                }
            }
        ]
        assert_resync_empty(sync, node, txmin=txmax)

    def test_label(self, sync, data):
        """There is no possible test for label at root level."""
        node = {
            'table': 'book',
            'label': 'some_label',
            'columns': ['isbn']
        }
        node

    def test_transform(self, sync, data):
        """Test transform for node attributes."""
        node = {
            'table': 'book',
            'columns': ['isbn', 'title', 'description'],
            'transform': {
                'rename': {
                    'isbn': 'book_isbn',
                    'title': 'book_title'
                }
            }
        }
        docs = [doc for doc in sync._sync(node)]
        assert docs == [
            {
                '_id': 'abc',
                '_index': 'testdb',
                '_source': {
                    '_meta': {},
                    'description': 'Tigers are fierce creatures',
                    'book_isbn': 'abc',
                    'book_title': 'The Tiger Club'
                }
            },
            {
                '_id': 'def',
                '_index': 'testdb',
                '_source': {
                    '_meta': {},
                    'description': 'Lion and the mouse',
                    'book_isbn': 'def',
                    'book_title': 'The Lion Club'
                }
            },
            {
                '_id': 'ghi',
                '_index': 'testdb',
                '_source': {
                    '_meta': {},
                    'description': 'Rabbits on the run',
                    'book_isbn': 'ghi',
                    'book_title': 'The Rabbit Club'
                }
            }
        ]
        assert_resync_empty(sync, node)

    def test_doc_includes_all_columns(self, sync, data):
        """Test the doc includes all selected columns."""
        node = {
            'table': 'book',
            'columns': ['isbn', 'title', 'description', 'xmin']
        }
        docs = [doc for doc in sync._sync(node)]
        assert sorted(docs[0]['_source'].keys()) == sorted([
            'isbn', 'title', 'description', 'xmin', '_meta'
        ])
        assert_resync_empty(sync, node)

    def test_select_xmin_column(self, sync, data):
        """Test the doc includes xmin column."""
        node = {
            'table': 'book',
            'columns': ['isbn', 'xmin']
        }
        docs = [doc for doc in sync._sync(node)]
        assert 'xmin' in docs[0]['_source']
        assert_resync_empty(sync, node)

    def test_no_column_specified(self, sync, data):
        """Test we include all columns when no columns are specified."""
        node = {
            'table': 'book',
            'columns': []
        }
        docs = [doc for doc in sync._sync(node)]
        assert sorted(docs[0]['_source'].keys()) == sorted([
            '_meta',
            'copyright',
            'description',
            'isbn',
            'publisher_id',
            'title'
        ])

        node = {
            'table': 'book'
        }
        docs = [doc for doc in sync._sync(node)]
        assert sorted(docs[0]['_source'].keys()) == sorted([
            '_meta',
            'copyright',
            'description',
            'isbn',
            'publisher_id',
            'title'
        ])
        assert_resync_empty(sync, node)

    def test_invalid_column(self, sync, data):
        """Test an invalid column raises ColumnNotFoundError."""
        node = {
            'table': 'book',
            'columns': ['foo']
        }
        with pytest.raises(ColumnNotFoundError) as excinfo:
            [doc for doc in sync._sync(node)]
        assert 'Column "foo" not present on table "book"' in str(excinfo.value)

    def test_primary_key_is_doc_id(self, sync, data):
        """Test the db primary key is used as the doc_id."""
        # TODO also repeat this test for composite primary key
        node = {
            'table': 'book',
            'columns': ['title']
        }
        docs = [doc for doc in sync._sync(node)]
        assert 'abc' == docs[0]['_id']
        assert 'def' == docs[1]['_id']
        assert 'ghi' == docs[2]['_id']
        assert_resync_empty(sync, node)

    def test_meta_in_docs(self, sync, data):
        """Test the private key is contained in the doc."""
        node = {
            'table': 'book',
            'columns': ['isbn']
        }
        docs = [doc for doc in sync._sync(node)]
        assert '_meta' in docs[0]['_source']
        assert_resync_empty(sync, node)

    def test_doc_only_includes_selected_columns(self, sync, data):
        """Ensure the doc only selected columns and builtins."""
        node = {
            'table': 'book',
            'columns': ['isbn', 'xmin']
        }
        docs = [doc for doc in sync._sync(node)]
        sources = {doc['_id']: doc['_source'] for doc in docs}

        assert sorted(
            sources['abc'].keys()
        ) == sorted(
            ['isbn', 'xmin', '_meta']
        )
        node = {
            'table': 'book',
            'columns': [
                'isbn',
                'xmin',
                'description',
                'copyright',
                'publisher_id',
            ]
        }

        docs = [doc for doc in sync._sync(node)]
        sources = {doc['_id']: doc['_source'] for doc in docs}

        assert sorted(
            sources['abc'].keys()
        ) == sorted([
            'isbn',
            'xmin',
            '_meta',
            'description',
            'copyright',
            'publisher_id',
        ])
        node = {
            'table': 'book',
            'columns': ['copyright', 'publisher_id']
        }

        docs = [doc for doc in sync._sync(node)]
        sources = {doc['_id']: doc['_source'] for doc in docs}

        assert sorted(
            sources['abc'].keys()) == sorted([
                '_meta',
                'copyright',
                'publisher_id',
            ])
        assert_resync_empty(sync, node)

    def test_doc_includes_nulls(self, sync, data):
        """Elasticsearch doc should include nulls from db."""
        node = {
            'table': 'book',
            'columns': ['isbn', 'description', 'copyright']
        }
        docs = [doc for doc in sync._sync(node)]
        sources = {doc['_id']: doc['_source'] for doc in docs}

        assert sources == {
            u'abc': {
                '_meta': {},
                u'copyright': None,
                u'description': u'Tigers are fierce creatures',
                u'isbn': u'abc'
            },
            u'def': {
                '_meta': {},
                u'copyright': None,
                u'description': u'Lion and the mouse',
                u'isbn': u'def'
            },
            u'ghi': {
                '_meta': {},
                u'copyright': None,
                u'description': u'Rabbits on the run',
                u'isbn': u'ghi'
            }
        }
        assert_resync_empty(sync, node)

    def test_meta_keys(self, sync, data):
        """Private keys should be included even if null."""
        node = {
            'table': 'book',
            'columns': ['description']
        }
        docs = [doc for doc in sync._sync(node)]
        sources = {doc['_id']: doc['_source'] for doc in docs}

        assert sources['abc']['_meta'] == {}
        assert sources['def']['_meta'] == {}
        assert sources['ghi']['_meta'] == {}
        assert_resync_empty(sync, node)

    def test_node_include_table(self, sync, data):
        """All node must include the table name."""
        node = {
            'no_table_specified': 'book',
            'columns': ['description']
        }
        with pytest.raises(TableNotInNodeError):
            [doc for doc in sync._sync(node)]

    def test_node_valid_attributes(self, sync, data):
        """All node must have valid attributes."""
        node = {
            'table': 'book',
            'unknown': 'xyz',
            'columns': ['description']
        }
        with pytest.raises(NodeAttributeError):
            [doc for doc in sync._sync(node)]

    def test_update_primary_key_non_concurrent(self, data, book_cls):
        """
        Test sync updates primary_key and then sync in non-concurrent mode.
        TODO: Note this test highlights a potential undesired bahaviour
        i.e we have a duplicate doc at a point in time.
        Note to self. I think this has been fixed. i.e we delete and then
        query ibsert
        """
        document = {
            'index': 'testdb',
            'node': {
                'table': 'book',
                'columns': ['isbn', 'title']
            }
        }
        sync = Sync(document)
        sync.sync()
        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]

        session = sync.session
        with subtransactions(session):
            session.execute(
                book_cls.__table__.update().where(
                    book_cls.__table__.c.isbn == 'abc'
                ).values(isbn='cba')
            )

        sync.sync()
        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'cba',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]
        assert_resync_empty(sync, document.get('node', {}))

    # TODO: Add another test like this and change
    # both primary key and non primary key column
    def test_update_primary_key_concurrent(self, data, book_cls):
        """Test sync updates primary_key and then sync in concurrent mode."""
        document = {
            'index': 'testdb',
            'node': {
                'table': 'book',
                'columns': ['isbn', 'title']
            }
        }
        sync = Sync(document)
        sync.sync()
        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]

        session = sync.session

        def pull():
            txmin = sync.checkpoint
            txmax = sync.txid_current
            sync.logical_slot_changes(txmin=txmin, txmax=txmax)

        def poll_redis():
            return []

        def poll_db():
            with subtransactions(session):
                session.execute(
                    book_cls.__table__.update().where(
                        book_cls.__table__.c.isbn == 'abc'
                    ).values(isbn='cba')
                )
                session.commit()

        with mock.patch('pgsync.sync.Sync.poll_redis', side_effect=poll_redis):
            with mock.patch('pgsync.sync.Sync.poll_db', side_effect=poll_db):
                with mock.patch('pgsync.sync.Sync.pull', side_effect=pull):
                    with mock.patch(
                        'pgsync.sync.Sync.truncate_slots',
                        side_effect=truncate_slots,
                    ):
                        sync.receive()
                        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert len(docs) == 3
        assert 'cba' in [doc['isbn'] for doc in docs]
        assert 'abc' not in [doc['isbn'] for doc in docs]

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'cba',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]
        assert_resync_empty(sync, document.get('node', {}))

    def test_insert_non_concurrent(self, data, book_cls):
        """Test sync insert and then sync in non-concurrent mode."""
        document = {
            'index': 'testdb',
            'node': {
                'table': 'book',
                'columns': ['isbn', 'title']
            }
        }
        sync = Sync(document)
        sync.sync()
        sync.es.refresh('testdb')

        session = sync.session

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]
        with subtransactions(session):
            session.execute(
                book_cls.__table__.insert().values(
                    isbn='xyz',
                    title='Encyclopedia'
                )
            )

        sync.sync()
        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            },
            {
                u'_meta': {},
                u'isbn': u'xyz',
                u'title': u'Encyclopedia'
            }
        ]
        assert_resync_empty(sync, document.get('node', {}))

    def test_update_non_concurrent(self, data, book_cls):
        """Test sync update and then sync in non-concurrent mode."""
        document = {
            'index': 'testdb',
            'node': {
                'table': 'book',
                'columns': ['isbn', 'title']
            }
        }
        sync = Sync(document)
        sync.sync()
        sync.es.refresh('testdb')

        session = sync.session

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]

        with subtransactions(session):
            session.execute(
                book_cls.__table__.update().where(
                    book_cls.__table__.c.isbn == 'abc'
                ).values(title='Tiger Club')
            )

        sync.sync()
        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]
        assert_resync_empty(sync, document.get('node', {}))

    def test_update_concurrent(self, data, book_cls):
        """Test sync update and then sync in concurrent mode."""
        document = {
            'index': 'testdb',
            'node': {
                'table': 'book',
                'columns': ['isbn', 'title']
            }
        }
        sync = Sync(document)
        sync.sync()
        sync.es.refresh('testdb')

        session = sync.session

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]

        def pull():
            txmin = sync.checkpoint
            txmax = sync.txid_current
            sync.logical_slot_changes(txmin=txmin, txmax=txmax)

        def poll_redis():
            return []

        def poll_db():
            with subtransactions(session):
                session.execute(
                    book_cls.__table__.update().where(
                        book_cls.__table__.c.isbn == 'abc'
                    ).values(title='Tiger Club')
                )
                session.commit()

        with mock.patch('pgsync.sync.Sync.poll_redis', side_effect=poll_redis):
            with mock.patch('pgsync.sync.Sync.poll_db', side_effect=poll_db):
                with mock.patch('pgsync.sync.Sync.pull', side_effect=pull):
                    with mock.patch(
                        'pgsync.sync.Sync.truncate_slots',
                        side_effect=truncate_slots,
                    ):
                        sync.receive()
                        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]
        assert_resync_empty(sync, document.get('node', {}))

    def test_delete_concurrent(self, data, book_cls):
        """Test sync delete and then sync in concurrent mode."""
        document = {
            'index': 'testdb',
            'node': {
                'table': 'book',
                'columns': ['isbn', 'title']
            }
        }
        sync = Sync(document)
        sync.sync()
        sync.es.refresh('testdb')
        session = sync.session
        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'abc',
                u'title': u'The Tiger Club'
            },
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]

        def pull():
            txmin = sync.checkpoint
            txmax = sync.txid_current
            sync.logical_slot_changes(txmin=txmin, txmax=txmax)

        def poll_redis():
            return []

        def poll_db():
            with subtransactions(session):
                session.execute(
                    book_cls.__table__.delete().where(
                        book_cls.__table__.c.isbn == 'abc'
                    )
                )
                session.commit()

        with mock.patch('pgsync.sync.Sync.poll_redis', side_effect=poll_redis):
            with mock.patch('pgsync.sync.Sync.poll_db', side_effect=poll_db):
                with mock.patch('pgsync.sync.Sync.pull', side_effect=pull):
                    with mock.patch(
                        'pgsync.sync.Sync.truncate_slots',
                        side_effect=truncate_slots,
                    ):
                        sync.receive()
                        sync.es.refresh('testdb')

        docs = search(sync.es, 'testdb')

        assert docs == [
            {
                u'_meta': {},
                u'isbn': u'def',
                u'title': u'The Lion Club'
            },
            {
                u'_meta': {},
                u'isbn': u'ghi',
                u'title': u'The Rabbit Club'
            }
        ]
        assert_resync_empty(sync, document.get('node', {}))

    def test_truncate(self, data, book_cls):
        """Test truncate."""
        # TODO: implement when truncate is supported
        pass
