import copy
import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import aggregate_news as aggregate
import main
import score_news as score
import split_news as split
from common import completion
from feishu import Feishu
from config import Config


def item(summary='Google 发布 AI 模型'):
    return {'公司': 'Google', '分类': '模型', '内容概括': summary}


def record(rid, **fields):
    return {'record_id': rid, 'fields': {**item(), **fields}}


def client_response(text, reason='stop'):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=reason)])
    return client


class MemoryAPI:
    def __init__(self, source=(), destination=()):
        self.config = SimpleNamespace(source_table='source', destination_table='dest',
                                      timezone='Asia/Shanghai', lookback_days=7)
        self.tables = {'source': {r['record_id']: copy.deepcopy(r) for r in source},
                       'dest': {r['record_id']: copy.deepcopy(r) for r in destination}}
        self.events = []
        self.fail_create_number = None
        self.create_count = 0
        self.fail_update = False
        self.fail_delete = False
        self.fail_mark = False
        self.ambiguous_create = False

    def validate_schema(self):
        pass

    def records(self, table):
        return copy.deepcopy(list(self.tables[table].values()))

    def create(self, table, fields):
        self.create_count += 1
        if self.create_count == self.fail_create_number:
            raise RuntimeError('create failed')
        rid = f'new{self.create_count}'
        self.tables[table][rid] = {'record_id': rid, 'fields': copy.deepcopy(fields)}
        self.events.append(('create', table, rid))
        if self.ambiguous_create:
            self.ambiguous_create = False
            raise TimeoutError('response lost after server commit')
        return rid

    def update(self, table, rid, fields):
        if self.fail_update or (self.fail_mark and fields.get('是否已清洗') == '是'):
            raise RuntimeError('update failed')
        self.tables[table][rid]['fields'].update(copy.deepcopy(fields))
        self.events.append(('update', table, rid, copy.deepcopy(fields)))

    def delete(self, table, ids):
        if self.fail_delete:
            raise RuntimeError('delete failed')
        for rid in ids:
            del self.tables[table][rid]
        self.events.append(('delete', table, list(ids)))


class SplitTests(unittest.TestCase):
    def test_rich_text_fields(self):
        fields = {'内容概括': [{'text': 'Google 发布模型'}], '公司': [{'text': 'Google'}],
                  '分类': '模型', '来源': [{'text': '新闻'}]}
        result = split.build_plan(fields, Mock())
        self.assertEqual(result[0]['公司'], 'Google')
        self.assertEqual(result[0]['来源'], '新闻')

    def test_partial_create_resumes_saved_plan(self):
        api = MemoryAPI([record('src')])
        api.fail_create_number = 2
        with patch.object(split, 'build_plan', return_value=[item('事件 A'), item('事件 B')]) as build:
            with self.assertRaises(RuntimeError):
                split.run_split(api, api.records('source'), Mock())
            self.assertNotIn('是否已清洗', api.tables['source']['src']['fields'])
            ids = split.run_split(api, api.records('source'), Mock())
            self.assertEqual(build.call_count, 1)
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(api.tables['dest']), 2)

    def test_create_timeout_after_commit_does_not_duplicate(self):
        api = MemoryAPI([record('src')])
        api.ambiguous_create = True
        with self.assertRaises(TimeoutError):
            split.run_split(api, api.records('source'), Mock())
        split.run_split(api, api.records('source'), Mock())
        self.assertEqual(len(api.tables['dest']), 1)
        self.assertEqual(api.create_count, 1)

    def test_empty_split_is_rejected_before_write(self):
        api = MemoryAPI([record('src', 内容概括='AI 日报', 链接='https://example.com/news')])
        with patch.object(split, 'fetch_article', return_value='正文'), \
             patch.object(split, 'split_with_deepseek', return_value=[]):
            with self.assertRaises(ValueError):
                split.run_split(api, api.records('source'), Mock())
        self.assertFalse(api.events)

    def test_failed_article_does_not_generate_from_title(self):
        with patch.object(split, 'fetch_article', side_effect=RuntimeError('blocked')), \
             patch.object(split, 'split_with_deepseek') as generate:
            with self.assertRaises(RuntimeError):
                split.build_plan({'内容概括': 'AI日报', '链接': 'https://example.com'}, Mock())
        generate.assert_not_called()


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.api = MemoryAPI(destination=[
            record('a', 清洗键='s1:0', 链接='https://a', 聚合链接='https://older', 清洗方式='拆分后聚合'),
            record('b', 清洗键='s2:0', 链接='https://b')])
        self.group = [{'primary_record_id': 'a', 'duplicate_record_ids': ['b']}]

    def merge(self):
        with patch.object(aggregate, 'cluster_with_deepseek', return_value=self.group), \
             patch.object(aggregate, 'completion', return_value='合并后的新闻'):
            return aggregate.run_aggregate(self.api, ['a', 'b'], Mock())

    def test_update_failure_never_deletes(self):
        self.api.fail_update = True
        with self.assertRaises(RuntimeError):
            self.merge()
        self.assertEqual(len(self.api.tables['dest']), 2)
        self.assertFalse(any(e[0] == 'delete' for e in self.api.events))

    def test_delete_failure_is_recoverable(self):
        self.api.fail_delete = True
        with self.assertRaises(RuntimeError):
            self.merge()
        self.assertTrue(self.api.tables['dest']['a']['fields']['待删除记录'])
        self.api.fail_delete = False
        aggregate.resume_pending_merges(self.api)
        fields = self.api.tables['dest']['a']['fields']
        self.assertEqual(fields['待删除记录'], '')
        self.assertEqual(set(fields['清洗键'].split()), {'s1:0', 's2:0'})
        self.assertEqual(set(fields['聚合链接'].split('、')), {'https://older', 'https://a', 'https://b'})
        self.assertEqual(fields['清洗方式'], '拆分后聚合')
        self.assertNotIn('b', self.api.tables['dest'])

    def test_resume_preserves_duplicate_edited_after_merge(self):
        self.api.fail_delete = True
        with self.assertRaises(RuntimeError):
            self.merge()
        self.api.tables['dest']['b']['fields']['内容概括'] = '人工添加的新信息'
        self.api.fail_delete = False
        with self.assertRaises(ValueError):
            aggregate.resume_pending_merges(self.api)
        self.assertIn('b', self.api.tables['dest'])

    def test_resume_after_delete_before_journal_clear(self):
        self.api.fail_delete = True
        with self.assertRaises(RuntimeError):
            self.merge()
        del self.api.tables['dest']['b']
        self.api.fail_delete = False
        aggregate.resume_pending_merges(self.api)
        self.assertEqual(self.api.tables['dest']['a']['fields']['待删除记录'], '')

    def test_model_ids_validated(self):
        records = self.api.records('dest')
        invalid = [
            [{'primary_record_id': 'a', 'duplicate_record_ids': ['a']}],
            [{'primary_record_id': 'a', 'duplicate_record_ids': ['unknown']}],
            self.group + [{'primary_record_id': 'b', 'duplicate_record_ids': ['a']}],
        ]
        for groups in invalid:
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                aggregate.validate_grouping({'groups': groups}, records)
        with self.assertRaises(ValueError):
            aggregate.validate_grouping({'groups': [], 'empty_record_ids': ['a']}, records)

    def test_cross_company_news_compared_together(self):
        self.api.tables['dest']['b']['fields']['公司'] = 'Google/OpenAI'
        client = client_response(json.dumps({'groups': self.group}))
        groups = aggregate.cluster_with_deepseek(self.api.records('dest'), client)
        self.assertEqual(groups, self.group)
        prompt = client.chat.completions.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('Google/OpenAI', prompt)
        self.assertIn('"record_id": "a"', prompt)

    def test_chinese_date_and_timezone(self):
        self.assertEqual(aggregate._parse_date('2026年09月29日'), date(2026, 9, 29))
        timestamp = datetime(2026, 9, 28, 0, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000
        self.assertEqual(aggregate._parse_date(timestamp), date(2026, 9, 28))
        self.assertEqual(aggregate._parse_date('12.31', today=date(2026, 1, 1)), date(2025, 12, 31))


class ScoreTests(unittest.TestCase):
    def test_api_failure_does_not_write_one(self):
        api = MemoryAPI(destination=[record('a')])
        client = Mock()
        client.chat.completions.create.side_effect = RuntimeError('rate limit')
        with self.assertRaises(RuntimeError):
            score.run_score(api, ['a'], client)
        self.assertNotIn('评分', api.tables['dest']['a']['fields'])

    def test_invalid_score_rejected(self):
        for value in ['5分', '15', '0', '', '4 or 5']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                score.score_with_deepseek(client_response(value), 'AI 新闻')

    def test_truncated_output_rejected(self):
        with self.assertRaises(ValueError):
            completion(client_response('[]', 'length'), 'prompt')

    def test_modified_content_and_links_are_rescored(self):
        fields = item()
        fields.update(评分=4, 评分依据=score.score_fingerprint(fields))
        self.assertFalse(score.needs_score(fields))
        fields['聚合链接'] = 'https://a、https://b'
        self.assertTrue(score.needs_score(fields))
        fields['评分依据'] = score.score_fingerprint(fields)
        fields['内容概括'] = '新的 AI 新闻'
        self.assertTrue(score.needs_score(fields))


class PipelineTests(unittest.TestCase):
    def test_scoring_failure_keeps_source_pending_and_retry_reuses_output(self):
        api = MemoryAPI([record('src')])
        failed_client = Mock()
        failed_client.chat.completions.create.side_effect = RuntimeError('model unavailable')
        with self.assertRaises(RuntimeError):
            main.run_pipeline(api, failed_client)
        self.assertNotIn('是否已清洗', api.tables['source']['src']['fields'])
        main.run_pipeline(api, client_response('4'))
        self.assertEqual(api.create_count, 1)
        self.assertEqual(api.tables['source']['src']['fields']['是否已清洗'], '是')
        self.assertEqual(api.tables['dest']['new1']['fields']['评分'], 4)

    def test_no_sources_still_retries_scoring(self):
        api = MemoryAPI(destination=[record('a', 时间='2020-01-01')])
        main.run_pipeline(api, client_response('3'))
        self.assertEqual(api.tables['dest']['a']['fields']['评分'], 3)

    def test_source_mark_failure_retry_after_merge_does_not_recreate_deleted_output(self):
        api = MemoryAPI([record('s1'), record('s2')])
        api.fail_mark = True
        groups = [{'primary_record_id': 'new1', 'duplicate_record_ids': ['new2']}]
        with patch.object(aggregate, 'cluster_with_deepseek', return_value=groups), \
             patch.object(aggregate, 'completion', return_value='合并后的新闻'):
            with self.assertRaises(RuntimeError):
                main.run_pipeline(api, client_response('4'))
        api.fail_mark = False
        main.run_pipeline(api, client_response('4'))
        self.assertEqual(api.create_count, 2)
        self.assertEqual(len(api.tables['dest']), 1)
        self.assertTrue(all(r['fields']['是否已清洗'] == '是' for r in api.tables['source'].values()))

    def test_rolling_window_includes_last_sunday_on_monday(self):
        fields = {**item(), '时间': '2026-09-27', '评分': 4}
        fields['评分依据'] = score.score_fingerprint(fields)
        api = MemoryAPI(destination=[{'record_id': 'a', 'fields': fields}])
        with patch.object(main, 'datetime') as clock, patch.object(main, 'run_aggregate', return_value=[]) as run:
            clock.now.return_value = datetime(2026, 9, 28, tzinfo=ZoneInfo('Asia/Shanghai'))
            main.run_pipeline(api, Mock())
        self.assertEqual(run.call_args.args[1], ['a'])


class APITests(unittest.TestCase):
    def test_schema_mismatch_stops_before_mutation(self):
        api = Feishu(SimpleNamespace(source_table='source', destination_table='dest'))
        api.request = Mock(return_value={'items': []})
        with self.assertRaises(ValueError):
            api.validate_schema()
        self.assertTrue(all(call.args[0] == 'GET' for call in api.request.call_args_list))

    def test_update_requires_matching_record_id(self):
        api = Feishu(SimpleNamespace())
        api.request = Mock(return_value={'record': {'record_id': 'other'}})
        with self.assertRaises(RuntimeError):
            api.update('dest', 'a', {'内容概括': 'test'})

    def test_http_success_business_failure_raises(self):
        response = Mock()
        response.json.return_value = {'code': 1254000}
        with self.assertRaises(RuntimeError):
            Feishu._checked(response)

    def test_partial_delete_response_raises(self):
        api = Feishu(SimpleNamespace())
        api.request = Mock(return_value={'records': [{'record_id': 'a', 'deleted': True},
                                                    {'record_id': 'b', 'deleted': False}]})
        with self.assertRaises(RuntimeError):
            api.delete('dest', ['a', 'b'])

    def test_pagination_without_cursor_raises(self):
        api = Feishu(SimpleNamespace())
        api.request = Mock(return_value={'items': [], 'has_more': True})
        with self.assertRaises(RuntimeError):
            api.records('dest')


class ConfigTests(unittest.TestCase):
    def test_missing_credentials_rejected(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(ValueError):
            Config.from_env()

    def test_source_destination_must_differ(self):
        values = {name: 'fake' for name in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_APP_TOKEN',
                  'FEISHU_SOURCE_TABLE_ID', 'FEISHU_DEST_TABLE_ID', 'DEEPSEEK_API_KEY')}
        with patch.dict('os.environ', values, clear=True), self.assertRaises(ValueError):
            Config.from_env()


if __name__ == '__main__':
    unittest.main()
