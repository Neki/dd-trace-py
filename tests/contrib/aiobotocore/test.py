import base64
import json

import aiobotocore
from botocore.errorfactory import ClientError

from ddtrace.compat import stringify
from ddtrace.constants import ANALYTICS_SAMPLE_RATE_KEY
from ddtrace.contrib.aiobotocore.patch import patch
from ddtrace.contrib.aiobotocore.patch import unpatch
from ddtrace.propagation.http import HTTP_HEADER_PARENT_ID
from ddtrace.propagation.http import HTTP_HEADER_TRACE_ID
from tests.utils import DummyTracer
from tests.utils import assert_is_measured
from tests.utils import assert_span_http_status_code

from ..asyncio.utils import AsyncioTestCase
from ..asyncio.utils import mark_asyncio
from .utils import aiobotocore_client
from .utils import get_zip_lambda


class AIOBotocoreTest(AsyncioTestCase):
    """Botocore integration testsuite"""

    def setUp(self):
        super(AIOBotocoreTest, self).setUp()
        patch()
        self.tracer = DummyTracer()

    def tearDown(self):
        super(AIOBotocoreTest, self).tearDown()
        unpatch()
        self.tracer = None

    @mark_asyncio
    def test_traced_client(self):
        with aiobotocore_client("ec2", self.tracer) as ec2:
            yield from ec2.describe_instances()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)
        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.get_tag("aws.agent"), "aiobotocore")
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "DescribeInstances")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.get_metric("retry_attempts"), 0)
        self.assertEqual(span.service, "aws.ec2")
        self.assertEqual(span.resource, "ec2.describeinstances")
        self.assertEqual(span.name, "ec2.command")
        self.assertEqual(span.span_type, "http")
        self.assertIsNone(span.get_metric(ANALYTICS_SAMPLE_RATE_KEY))

    @mark_asyncio
    def test_traced_client_analytics(self):
        with self.override_config("aiobotocore", dict(analytics_enabled=True, analytics_sample_rate=0.5)):
            with aiobotocore_client("ec2", self.tracer) as ec2:
                yield from ec2.describe_instances()

        spans = self.get_spans()
        assert spans
        span = spans[0]
        self.assertEqual(span.get_metric(ANALYTICS_SAMPLE_RATE_KEY), 0.5)

    @mark_asyncio
    def test_s3_client(self):
        with aiobotocore_client("s3", self.tracer) as s3:
            yield from s3.list_buckets()
            yield from s3.list_buckets()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 2)
        self.assertEqual(len(traces[0]), 1)
        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.get_tag("aws.operation"), "ListBuckets")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.service, "aws.s3")
        self.assertEqual(span.resource, "s3.listbuckets")
        self.assertEqual(span.name, "s3.command")

    @mark_asyncio
    def test_s3_put(self):
        params = dict(Key="foo", Bucket="mybucket", Body=b"bar")

        with aiobotocore_client("s3", self.tracer) as s3:
            yield from s3.create_bucket(Bucket="mybucket")
            yield from s3.put_object(**params)

        spans = [trace[0] for trace in self.pop_traces()]
        assert spans
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0].get_tag("aws.operation"), "CreateBucket")

        assert_is_measured(spans[0])
        assert_span_http_status_code(spans[0], 200)
        self.assertEqual(spans[0].service, "aws.s3")
        self.assertEqual(spans[0].resource, "s3.createbucket")

        assert_is_measured(spans[1])
        self.assertEqual(spans[1].get_tag("aws.operation"), "PutObject")
        self.assertEqual(spans[1].resource, "s3.putobject")
        self.assertEqual(spans[1].get_tag("params.Key"), stringify(params["Key"]))
        self.assertEqual(spans[1].get_tag("params.Bucket"), stringify(params["Bucket"]))
        self.assertIsNone(spans[1].get_tag("params.Body"))

    @mark_asyncio
    def test_s3_client_error(self):
        with aiobotocore_client("s3", self.tracer) as s3:
            with self.assertRaises(ClientError):
                # FIXME: add proper clean-up to tearDown
                yield from s3.list_objects(Bucket="doesnotexist")

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)
        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.resource, "s3.listobjects")
        self.assertEqual(span.error, 1)
        self.assertTrue("NoSuchBucket" in span.get_tag("error.msg"))

    @mark_asyncio
    def test_s3_client_read(self):
        with aiobotocore_client("s3", self.tracer) as s3:
            # prepare S3 and flush traces if any
            yield from s3.create_bucket(Bucket="tracing")
            yield from s3.put_object(Bucket="tracing", Key="apm", Body=b"")
            self.pop_traces()
            # calls under test
            response = yield from s3.get_object(Bucket="tracing", Key="apm")
            yield from response["Body"].read()

        traces = self.pop_traces()
        version = aiobotocore.__version__.split(".")
        pre_08 = int(version[0]) == 0 and int(version[1]) < 8
        if pre_08:
            self.assertEqual(len(traces), 2)
            self.assertEqual(len(traces[1]), 1)
        else:
            self.assertEqual(len(traces), 1)

        self.assertEqual(len(traces[0]), 1)

        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.get_tag("aws.operation"), "GetObject")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.service, "aws.s3")
        self.assertEqual(span.resource, "s3.getobject")

        if pre_08:
            read_span = traces[1][0]
            self.assertEqual(read_span.get_tag("aws.operation"), "GetObject")
            assert_span_http_status_code(read_span, 200)
            self.assertEqual(read_span.service, "aws.s3")
            self.assertEqual(read_span.resource, "s3.getobject")
            self.assertEqual(read_span.name, "s3.command.read")
            # enforce parenting
            self.assertEqual(read_span.parent_id, span.span_id)
            self.assertEqual(read_span.trace_id, span.trace_id)

    @mark_asyncio
    def test_sqs_client(self):
        with aiobotocore_client("sqs", self.tracer) as sqs:
            yield from sqs.list_queues()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)

        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "ListQueues")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.service, "aws.sqs")
        self.assertEqual(span.resource, "sqs.listqueues")

    @mark_asyncio
    def test_kinesis_client(self):
        with aiobotocore_client("kinesis", self.tracer) as kinesis:
            yield from kinesis.list_streams()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)

        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "ListStreams")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.service, "aws.kinesis")
        self.assertEqual(span.resource, "kinesis.liststreams")

    @mark_asyncio
    def test_lambda_client(self):
        with aiobotocore_client("lambda", self.tracer) as lambda_client:
            # https://github.com/spulec/moto/issues/906
            yield from lambda_client.list_functions(MaxItems=5)

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)

        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "ListFunctions")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.service, "aws.lambda")
        self.assertEqual(span.resource, "lambda.listfunctions")

    @mark_asyncio
    def test_lambda_invoke_no_context_client(self):
        with aiobotocore_client("lambda", self.tracer) as lambda_client:
            yield from lambda_client.create_function(
                FunctionName="ironmaiden",
                Runtime="python3.7",
                Role="test-iam-role",
                Handler="lambda_function.lambda_handler",
                Code={
                    "ZipFile": get_zip_lambda(),
                },
                Publish=True,
                Timeout=30,
                MemorySize=128,
            )

            # we are not interested in the traces of the previous call
            self.reset()

            yield from lambda_client.invoke(FunctionName="ironmaiden", Payload=json.dumps({}))

        spans = self.get_spans()
        assert spans
        span = spans[0]

        self.assertEqual(len(spans), 1)
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "Invoke")
        assert_is_measured(span)
        assert_span_http_status_code(span, 202)
        self.assertEqual(span.service, "aws.lambda")
        self.assertEqual(span.resource, "lambda.invoke")
        context_b64 = span.get_tag("params.ClientContext")
        context_json = base64.b64decode(context_b64.encode()).decode()
        context_obj = json.loads(context_json)

        self.assertEqual(context_obj["custom"]["_datadog"][HTTP_HEADER_TRACE_ID], str(span.trace_id))
        self.assertEqual(context_obj["custom"]["_datadog"][HTTP_HEADER_PARENT_ID], str(span.span_id))

        with aiobotocore_client("lambda", self.tracer) as lambda_client:
            yield from lambda_client.delete_function(FunctionName="ironmaiden")

    @mark_asyncio
    def test_lambda_invoke_distributed_tracing_off(self):
        with self.override_config("aiobotocore", dict(distributed_tracing=False)):
            with aiobotocore_client("lambda", self.tracer) as lambda_client:
                yield from lambda_client.create_function(
                    FunctionName="ironmaiden",
                    Runtime="python3.7",
                    Role="test-iam-role",
                    Handler="lambda_function.lambda_handler",
                    Code={
                        "ZipFile": get_zip_lambda(),
                    },
                    Publish=True,
                    Timeout=30,
                    MemorySize=128,
                )

                # we are not interested in the traces of the previous call
                self.reset()

                yield from lambda_client.invoke(FunctionName="ironmaiden", Payload=json.dumps({}))

            spans = self.get_spans()
            assert spans
            span = spans[0]

            self.assertEqual(len(spans), 1)
            self.assertEqual(span.get_tag("aws.region"), "us-west-2")
            self.assertEqual(span.get_tag("aws.operation"), "Invoke")
            assert_is_measured(span)
            assert_span_http_status_code(span, 202)
            self.assertEqual(span.service, "aws.lambda")
            self.assertEqual(span.resource, "lambda.invoke")
            self.assertEqual(span.get_tag("params.ClientContext"), None)

            with aiobotocore_client("lambda", self.tracer) as lambda_client:
                yield from lambda_client.delete_function(FunctionName="ironmaiden")

    @mark_asyncio
    def test_lambda_invoke_with_context_client(self):
        with aiobotocore_client("lambda", self.tracer) as lambda_client:
            yield from lambda_client.create_function(
                FunctionName="megadeth",
                Runtime="python3.7",
                Role="test-iam-role",
                Handler="lambda_function.lambda_handler",
                Code={
                    "ZipFile": get_zip_lambda(),
                },
                Publish=True,
                Timeout=30,
                MemorySize=128,
            )
            client_context = base64.b64encode(json.dumps({"custom": {"foo": "bar"}}).encode()).decode()

            # we are not interested in the traces of the previous call
            self.reset()

            yield from lambda_client.invoke(
                FunctionName="megadeth", ClientContext=client_context, Payload=json.dumps({})
            )

        spans = self.get_spans()
        assert spans
        span = spans[0]

        self.assertEqual(len(spans), 1)
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "Invoke")
        assert_is_measured(span)
        assert_span_http_status_code(span, 202)
        self.assertEqual(span.service, "aws.lambda")
        self.assertEqual(span.resource, "lambda.invoke")
        context_b64 = span.get_tag("params.ClientContext")
        context_json = base64.b64decode(context_b64.encode()).decode()
        context_obj = json.loads(context_json)

        self.assertEqual(context_obj["custom"]["foo"], "bar")
        self.assertEqual(context_obj["custom"]["_datadog"][HTTP_HEADER_TRACE_ID], str(span.trace_id))
        self.assertEqual(context_obj["custom"]["_datadog"][HTTP_HEADER_PARENT_ID], str(span.span_id))

        with aiobotocore_client("lambda", self.tracer) as lambda_client:
            yield from lambda_client.delete_function(FunctionName="megadeth")

    @mark_asyncio
    def test_lambda_invoke_bad_context_client(self):
        with aiobotocore_client("lambda", self.tracer) as lambda_client:
            yield from lambda_client.create_function(
                FunctionName="black-sabbath",
                Runtime="python3.7",
                Role="test-iam-role",
                Handler="lambda_function.lambda_handler",
                Code={
                    "ZipFile": get_zip_lambda(),
                },
                Publish=True,
                Timeout=30,
                MemorySize=128,
            )

            # we are not interested in the traces of the previous call
            self.reset()

            yield from lambda_client.invoke(
                FunctionName="black-sabbath",
                ClientContext="bad_client_context",
                Payload=json.dumps({}),
            )

        spans = self.get_spans()
        assert spans
        span = spans[0]
        self.assertEqual(len(spans), 1)
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "Invoke")
        assert_is_measured(span)

        with aiobotocore_client("lambda", self.tracer) as lambda_client:
            yield from lambda_client.delete_function(FunctionName="black-sabbath")

    @mark_asyncio
    def test_kms_client(self):
        with aiobotocore_client("kms", self.tracer) as kms:
            yield from kms.list_keys(Limit=21)

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)

        span = traces[0][0]

        assert_is_measured(span)
        self.assertEqual(span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(span.get_tag("aws.operation"), "ListKeys")
        assert_span_http_status_code(span, 200)
        self.assertEqual(span.service, "aws.kms")
        self.assertEqual(span.resource, "kms.listkeys")
        # checking for protection on STS against security leak
        self.assertEqual(span.get_tag("params"), None)

    @mark_asyncio
    def test_unpatch(self):
        unpatch()
        with aiobotocore_client("kinesis", self.tracer) as kinesis:
            yield from kinesis.list_streams()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 0)

    @mark_asyncio
    def test_double_patch(self):
        patch()
        with aiobotocore_client("sqs", self.tracer) as sqs:
            yield from sqs.list_queues()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)

    @mark_asyncio
    def test_opentraced_client(self):
        from tests.opentracer.utils import init_tracer

        ot_tracer = init_tracer("my_svc", self.tracer)

        with ot_tracer.start_active_span("ot_outer_span"):
            with aiobotocore_client("ec2", self.tracer) as ec2:
                yield from ec2.describe_instances()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 2)
        ot_span = traces[0][0]
        dd_span = traces[0][1]

        self.assertEqual(ot_span.resource, "ot_outer_span")
        self.assertEqual(ot_span.service, "my_svc")

        # confirm the parenting
        self.assertEqual(ot_span.parent_id, None)
        self.assertEqual(dd_span.parent_id, ot_span.span_id)

        assert_is_measured(dd_span)
        self.assertEqual(dd_span.get_tag("aws.agent"), "aiobotocore")
        self.assertEqual(dd_span.get_tag("aws.region"), "us-west-2")
        self.assertEqual(dd_span.get_tag("aws.operation"), "DescribeInstances")
        assert_span_http_status_code(dd_span, 200)
        self.assertEqual(dd_span.get_metric("retry_attempts"), 0)
        self.assertEqual(dd_span.service, "aws.ec2")
        self.assertEqual(dd_span.resource, "ec2.describeinstances")
        self.assertEqual(dd_span.name, "ec2.command")

    @mark_asyncio
    def test_opentraced_s3_client(self):
        from tests.opentracer.utils import init_tracer

        ot_tracer = init_tracer("my_svc", self.tracer)

        with ot_tracer.start_active_span("ot_outer_span"):
            with aiobotocore_client("s3", self.tracer) as s3:
                yield from s3.list_buckets()
                with ot_tracer.start_active_span("ot_inner_span1"):
                    yield from s3.list_buckets()
                with ot_tracer.start_active_span("ot_inner_span2"):
                    pass

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 5)
        ot_outer_span = traces[0][0]
        dd_span = traces[0][1]
        ot_inner_span = traces[0][2]
        dd_span2 = traces[0][3]
        ot_inner_span2 = traces[0][4]

        self.assertEqual(ot_outer_span.resource, "ot_outer_span")
        self.assertEqual(ot_inner_span.resource, "ot_inner_span1")
        self.assertEqual(ot_inner_span2.resource, "ot_inner_span2")

        # confirm the parenting
        self.assertEqual(ot_outer_span.parent_id, None)
        self.assertEqual(dd_span.parent_id, ot_outer_span.span_id)
        self.assertEqual(ot_inner_span.parent_id, ot_outer_span.span_id)
        self.assertEqual(dd_span2.parent_id, ot_inner_span.span_id)
        self.assertEqual(ot_inner_span2.parent_id, ot_outer_span.span_id)

        assert_is_measured(dd_span)
        self.assertEqual(dd_span.get_tag("aws.operation"), "ListBuckets")
        assert_span_http_status_code(dd_span, 200)
        self.assertEqual(dd_span.service, "aws.s3")
        self.assertEqual(dd_span.resource, "s3.listbuckets")
        self.assertEqual(dd_span.name, "s3.command")

        self.assertEqual(dd_span2.get_tag("aws.operation"), "ListBuckets")
        assert_span_http_status_code(dd_span2, 200)
        self.assertEqual(dd_span2.service, "aws.s3")
        self.assertEqual(dd_span2.resource, "s3.listbuckets")
        self.assertEqual(dd_span2.name, "s3.command")

    @mark_asyncio
    @AsyncioTestCase.run_in_subprocess(env_overrides=dict(DD_SERVICE="mysvc"))
    def test_user_specified_service(self):
        """
        When a service name is specified by the user
            The aiobotocore integration should use it as the service name
        """
        with aiobotocore_client("ec2", self.tracer) as ec2:
            yield from ec2.describe_instances()

        traces = self.pop_traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)
        span = traces[0][0]

        assert span.service == "mysvc"
