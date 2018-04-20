# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

import simplejson as json
from twisted.internet import defer

from synapse.api.errors import AuthError, FederationError, SynapseError, NotFoundError
from synapse.crypto.event_signing import compute_event_signature
from synapse.federation.federation_base import (
    FederationBase,
    event_from_pdu_json,
)

from synapse.federation.persistence import TransactionActions
from synapse.federation.units import Edu, Transaction
import synapse.metrics
from synapse.types import get_domain_from_id
from synapse.util import async
from synapse.util.caches.response_cache import ResponseCache
from synapse.util.logutils import log_function

# when processing incoming transactions, we try to handle multiple rooms in
# parallel, up to this limit.
TRANSACTION_CONCURRENCY_LIMIT = 10

logger = logging.getLogger(__name__)

# synapse.federation.federation_server is a silly name
metrics = synapse.metrics.get_metrics_for("synapse.federation.server")

received_pdus_counter = metrics.register_counter("received_pdus")

received_edus_counter = metrics.register_counter("received_edus")

received_queries_counter = metrics.register_counter("received_queries", labels=["type"])


class FederationServer(FederationBase):
    def __init__(self, hs):
        super(FederationServer, self).__init__(hs)

        self.auth = hs.get_auth()
        self.handler = hs.get_handlers().federation_handler

        self._server_linearizer = async.Linearizer("fed_server")
        self._transaction_linearizer = async.Linearizer("fed_txn_handler")

        self.transaction_actions = TransactionActions(self.store)

        self.registry = hs.get_federation_registry()

        # We cache responses to state queries, as they take a while and often
        # come in waves.
        self._state_resp_cache = ResponseCache(hs, "state_resp", timeout_ms=30000)

    @defer.inlineCallbacks
    @log_function
    def on_backfill_request(self, origin, room_id, versions, limit):
        with (yield self._server_linearizer.queue((origin, room_id))):
            pdus = yield self.handler.on_backfill_request(
                origin, room_id, versions, limit
            )

            res = self._transaction_from_pdus(pdus).get_dict()

        defer.returnValue((200, res))

    @defer.inlineCallbacks
    @log_function
    def on_incoming_transaction(self, transaction_data):
        # keep this as early as possible to make the calculated origin ts as
        # accurate as possible.
        request_time = self._clock.time_msec()

        transaction = Transaction(**transaction_data)

        if not transaction.transaction_id:
            raise Exception("Transaction missing transaction_id")
        if not transaction.origin:
            raise Exception("Transaction missing origin")

        logger.debug("[%s] Got transaction", transaction.transaction_id)

        # use a linearizer to ensure that we don't process the same transaction
        # multiple times in parallel.
        with (yield self._transaction_linearizer.queue(
                (transaction.origin, transaction.transaction_id),
        )):
            result = yield self._handle_incoming_transaction(
                transaction, request_time,
            )

        defer.returnValue(result)

    @defer.inlineCallbacks
    def _handle_incoming_transaction(self, transaction, request_time):
        """ Process an incoming transaction and return the HTTP response

        Args:
            transaction (Transaction): incoming transaction
            request_time (int): timestamp that the HTTP request arrived at

        Returns:
            Deferred[(int, object)]: http response code and body
        """
        response = yield self.transaction_actions.have_responded(transaction)

        if response:
            logger.debug(
                "[%s] We've already responded to this request",
                transaction.transaction_id
            )
            defer.returnValue(response)
            return

        logger.debug("[%s] Transaction is new", transaction.transaction_id)

        received_pdus_counter.inc_by(len(transaction.pdus))

        pdus_by_room = {}

        for p in transaction.pdus:
            if "unsigned" in p:
                unsigned = p["unsigned"]
                if "age" in unsigned:
                    p["age"] = unsigned["age"]
            if "age" in p:
                p["age_ts"] = request_time - int(p["age"])
                del p["age"]

            event = event_from_pdu_json(p)
            room_id = event.room_id
            pdus_by_room.setdefault(room_id, []).append(event)

        pdu_results = {}

        # we can process different rooms in parallel (which is useful if they
        # require callouts to other servers to fetch missing events), but
        # impose a limit to avoid going too crazy with ram/cpu.
        @defer.inlineCallbacks
        def process_pdus_for_room(room_id):
            logger.debug("Processing PDUs for %s", room_id)
            for pdu in pdus_by_room[room_id]:
                event_id = pdu.event_id
                try:
                    yield self._handle_received_pdu(
                        transaction.origin, pdu
                    )
                    pdu_results[event_id] = {}
                except FederationError as e:
                    logger.warn("Error handling PDU %s: %s", event_id, e)
                    pdu_results[event_id] = {"error": str(e)}
                except Exception as e:
                    pdu_results[event_id] = {"error": str(e)}
                    logger.exception("Failed to handle PDU %s", event_id)

        yield async.concurrently_execute(
            process_pdus_for_room, pdus_by_room.keys(),
            TRANSACTION_CONCURRENCY_LIMIT,
        )

        if hasattr(transaction, "edus"):
            for edu in (Edu(**x) for x in transaction.edus):
                yield self.received_edu(
                    transaction.origin,
                    edu.edu_type,
                    edu.content
                )

        pdu_failures = getattr(transaction, "pdu_failures", [])
        for failure in pdu_failures:
            logger.info("Got failure %r", failure)

        response = {
            "pdus": pdu_results,
        }

        logger.debug("Returning: %s", str(response))

        yield self.transaction_actions.set_response(
            transaction,
            200, response
        )
        defer.returnValue((200, response))

    @defer.inlineCallbacks
    def received_edu(self, origin, edu_type, content):
        received_edus_counter.inc()
        yield self.registry.on_edu(edu_type, origin, content)

    @defer.inlineCallbacks
    @log_function
    def on_context_state_request(self, origin, room_id, event_id):
        if not event_id:
            raise NotImplementedError("Specify an event")

        in_room = yield self.auth.check_host_in_room(room_id, origin)
        if not in_room:
            raise AuthError(403, "Host not in room.")

        # we grab the linearizer to protect ourselves from servers which hammer
        # us. In theory we might already have the response to this query
        # in the cache so we could return it without waiting for the linearizer
        # - but that's non-trivial to get right, and anyway somewhat defeats
        # the point of the linearizer.
        with (yield self._server_linearizer.queue((origin, room_id))):
            resp = yield self._state_resp_cache.wrap(
                (room_id, event_id),
                self._on_context_state_request_compute,
                room_id, event_id,
            )

        defer.returnValue((200, resp))

    @defer.inlineCallbacks
    def on_state_ids_request(self, origin, room_id, event_id):
        if not event_id:
            raise NotImplementedError("Specify an event")

        in_room = yield self.auth.check_host_in_room(room_id, origin)
        if not in_room:
            raise AuthError(403, "Host not in room.")

        state_ids = yield self.handler.get_state_ids_for_pdu(
            room_id, event_id,
        )
        auth_chain_ids = yield self.store.get_auth_chain_ids(state_ids)

        defer.returnValue((200, {
            "pdu_ids": state_ids,
            "auth_chain_ids": auth_chain_ids,
        }))

    @defer.inlineCallbacks
    def _on_context_state_request_compute(self, room_id, event_id):
        pdus = yield self.handler.get_state_for_pdu(
            room_id, event_id,
        )
        auth_chain = yield self.store.get_auth_chain(
            [pdu.event_id for pdu in pdus]
        )

        for event in auth_chain:
            # We sign these again because there was a bug where we
            # incorrectly signed things the first time round
            if self.hs.is_mine_id(event.event_id):
                event.signatures.update(
                    compute_event_signature(
                        event,
                        self.hs.hostname,
                        self.hs.config.signing_key[0]
                    )
                )

        defer.returnValue({
            "pdus": [pdu.get_pdu_json() for pdu in pdus],
            "auth_chain": [pdu.get_pdu_json() for pdu in auth_chain],
        })

    @defer.inlineCallbacks
    @log_function
    def on_pdu_request(self, origin, event_id):
        pdu = yield self._get_persisted_pdu(origin, event_id)

        if pdu:
            defer.returnValue(
                (200, self._transaction_from_pdus([pdu]).get_dict())
            )
        else:
            defer.returnValue((404, ""))

    @defer.inlineCallbacks
    @log_function
    def on_pull_request(self, origin, versions):
        raise NotImplementedError("Pull transactions not implemented")

    @defer.inlineCallbacks
    def on_query_request(self, query_type, args):
        received_queries_counter.inc(query_type)
        resp = yield self.registry.on_query(query_type, args)
        defer.returnValue((200, resp))

    @defer.inlineCallbacks
    def on_make_join_request(self, room_id, user_id):
        pdu = yield self.handler.on_make_join_request(room_id, user_id)
        time_now = self._clock.time_msec()
        defer.returnValue({"event": pdu.get_pdu_json(time_now)})

    @defer.inlineCallbacks
    def on_invite_request(self, origin, content):
        pdu = event_from_pdu_json(content)
        ret_pdu = yield self.handler.on_invite_request(origin, pdu)
        time_now = self._clock.time_msec()
        defer.returnValue((200, {"event": ret_pdu.get_pdu_json(time_now)}))

    @defer.inlineCallbacks
    def on_send_join_request(self, origin, content):
        logger.debug("on_send_join_request: content: %s", content)
        pdu = event_from_pdu_json(content)
        logger.debug("on_send_join_request: pdu sigs: %s", pdu.signatures)
        res_pdus = yield self.handler.on_send_join_request(origin, pdu)
        time_now = self._clock.time_msec()
        defer.returnValue((200, {
            "state": [p.get_pdu_json(time_now) for p in res_pdus["state"]],
            "auth_chain": [
                p.get_pdu_json(time_now) for p in res_pdus["auth_chain"]
            ],
        }))

    @defer.inlineCallbacks
    def on_make_leave_request(self, room_id, user_id):
        pdu = yield self.handler.on_make_leave_request(room_id, user_id)
        time_now = self._clock.time_msec()
        defer.returnValue({"event": pdu.get_pdu_json(time_now)})

    @defer.inlineCallbacks
    def on_send_leave_request(self, origin, content):
        logger.debug("on_send_leave_request: content: %s", content)
        pdu = event_from_pdu_json(content)
        logger.debug("on_send_leave_request: pdu sigs: %s", pdu.signatures)
        yield self.handler.on_send_leave_request(origin, pdu)
        defer.returnValue((200, {}))

    @defer.inlineCallbacks
    def on_event_auth(self, origin, room_id, event_id):
        with (yield self._server_linearizer.queue((origin, room_id))):
            time_now = self._clock.time_msec()
            auth_pdus = yield self.handler.on_event_auth(event_id)
            res = {
                "auth_chain": [a.get_pdu_json(time_now) for a in auth_pdus],
            }
        defer.returnValue((200, res))

    @defer.inlineCallbacks
    def on_query_auth_request(self, origin, content, room_id, event_id):
        """
        Content is a dict with keys::
            auth_chain (list): A list of events that give the auth chain.
            missing (list): A list of event_ids indicating what the other
              side (`origin`) think we're missing.
            rejects (dict): A mapping from event_id to a 2-tuple of reason
              string and a proof (or None) of why the event was rejected.
              The keys of this dict give the list of events the `origin` has
              rejected.

        Args:
            origin (str)
            content (dict)
            event_id (str)

        Returns:
            Deferred: Results in `dict` with the same format as `content`
        """
        with (yield self._server_linearizer.queue((origin, room_id))):
            auth_chain = [
                event_from_pdu_json(e)
                for e in content["auth_chain"]
            ]

            signed_auth = yield self._check_sigs_and_hash_and_fetch(
                origin, auth_chain, outlier=True
            )

            ret = yield self.handler.on_query_auth(
                origin,
                event_id,
                signed_auth,
                content.get("rejects", []),
                content.get("missing", []),
            )

            time_now = self._clock.time_msec()
            send_content = {
                "auth_chain": [
                    e.get_pdu_json(time_now)
                    for e in ret["auth_chain"]
                ],
                "rejects": ret.get("rejects", []),
                "missing": ret.get("missing", []),
            }

        defer.returnValue(
            (200, send_content)
        )

    @log_function
    def on_query_client_keys(self, origin, content):
        return self.on_query_request("client_keys", content)

    def on_query_user_devices(self, origin, user_id):
        return self.on_query_request("user_devices", user_id)

    @defer.inlineCallbacks
    @log_function
    def on_claim_client_keys(self, origin, content):
        query = []
        for user_id, device_keys in content.get("one_time_keys", {}).items():
            for device_id, algorithm in device_keys.items():
                query.append((user_id, device_id, algorithm))

        results = yield self.store.claim_e2e_one_time_keys(query)

        json_result = {}
        for user_id, device_keys in results.items():
            for device_id, keys in device_keys.items():
                for key_id, json_bytes in keys.items():
                    json_result.setdefault(user_id, {})[device_id] = {
                        key_id: json.loads(json_bytes)
                    }

        logger.info(
            "Claimed one-time-keys: %s",
            ",".join((
                "%s for %s:%s" % (key_id, user_id, device_id)
                for user_id, user_keys in json_result.iteritems()
                for device_id, device_keys in user_keys.iteritems()
                for key_id, _ in device_keys.iteritems()
            )),
        )

        defer.returnValue({"one_time_keys": json_result})

    @defer.inlineCallbacks
    @log_function
    def on_get_missing_events(self, origin, room_id, earliest_events,
                              latest_events, limit, min_depth):
        with (yield self._server_linearizer.queue((origin, room_id))):
            logger.info(
                "on_get_missing_events: earliest_events: %r, latest_events: %r,"
                " limit: %d, min_depth: %d",
                earliest_events, latest_events, limit, min_depth
            )

            missing_events = yield self.handler.on_get_missing_events(
                origin, room_id, earliest_events, latest_events, limit, min_depth
            )

            if len(missing_events) < 5:
                logger.info(
                    "Returning %d events: %r", len(missing_events), missing_events
                )
            else:
                logger.info("Returning %d events", len(missing_events))

            time_now = self._clock.time_msec()

        defer.returnValue({
            "events": [ev.get_pdu_json(time_now) for ev in missing_events],
        })

    @log_function
    def on_openid_userinfo(self, token):
        ts_now_ms = self._clock.time_msec()
        return self.store.get_user_id_for_open_id_token(token, ts_now_ms)

    @log_function
    def _get_persisted_pdu(self, origin, event_id, do_auth=True):
        """ Get a PDU from the database with given origin and id.

        Returns:
            Deferred: Results in a `Pdu`.
        """
        return self.handler.get_persisted_pdu(
            origin, event_id, do_auth=do_auth
        )

    def _transaction_from_pdus(self, pdu_list):
        """Returns a new Transaction containing the given PDUs suitable for
        transmission.
        """
        time_now = self._clock.time_msec()
        pdus = [p.get_pdu_json(time_now) for p in pdu_list]
        return Transaction(
            origin=self.server_name,
            pdus=pdus,
            origin_server_ts=int(time_now),
            destination=None,
        )

    @defer.inlineCallbacks
    def _handle_received_pdu(self, origin, pdu):
        """ Process a PDU received in a federation /send/ transaction.

        Args:
            origin (str): server which sent the pdu
            pdu (FrozenEvent): received pdu

        Returns (Deferred): completes with None
        Raises: FederationError if the signatures / hash do not match
    """
        # check that it's actually being sent from a valid destination to
        # workaround bug #1753 in 0.18.5 and 0.18.6
        if origin != get_domain_from_id(pdu.event_id):
            # We continue to accept join events from any server; this is
            # necessary for the federation join dance to work correctly.
            # (When we join over federation, the "helper" server is
            # responsible for sending out the join event, rather than the
            # origin. See bug #1893).
            if not (
                pdu.type == 'm.room.member' and
                pdu.content and
                pdu.content.get("membership", None) == 'join'
            ):
                logger.info(
                    "Discarding PDU %s from invalid origin %s",
                    pdu.event_id, origin
                )
                return
            else:
                logger.info(
                    "Accepting join PDU %s from %s",
                    pdu.event_id, origin
                )

        # Check signature.
        try:
            pdu = yield self._check_sigs_and_hash(pdu)
        except SynapseError as e:
            raise FederationError(
                "ERROR",
                e.code,
                e.msg,
                affected=pdu.event_id,
            )

        yield self.handler.on_receive_pdu(origin, pdu, get_missing=True)

    def __str__(self):
        return "<ReplicationLayer(%s)>" % self.server_name

    @defer.inlineCallbacks
    def exchange_third_party_invite(
            self,
            sender_user_id,
            target_user_id,
            room_id,
            signed,
    ):
        ret = yield self.handler.exchange_third_party_invite(
            sender_user_id,
            target_user_id,
            room_id,
            signed,
        )
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def on_exchange_third_party_invite_request(self, origin, room_id, event_dict):
        ret = yield self.handler.on_exchange_third_party_invite_request(
            origin, room_id, event_dict
        )
        defer.returnValue(ret)


class FederationHandlerRegistry(object):
    """Allows classes to register themselves as handlers for a given EDU or
    query type for incoming federation traffic.
    """
    def __init__(self):
        self.edu_handlers = {}
        self.query_handlers = {}

    def register_edu_handler(self, edu_type, handler):
        """Sets the handler callable that will be used to handle an incoming
        federation EDU of the given type.

        Args:
            edu_type (str): The type of the incoming EDU to register handler for
            handler (Callable[[str, dict]]): A callable invoked on incoming EDU
                of the given type. The arguments are the origin server name and
                the EDU contents.
        """
        if edu_type in self.edu_handlers:
            raise KeyError("Already have an EDU handler for %s" % (edu_type,))

        self.edu_handlers[edu_type] = handler

    def register_query_handler(self, query_type, handler):
        """Sets the handler callable that will be used to handle an incoming
        federation query of the given type.

        Args:
            query_type (str): Category name of the query, which should match
                the string used by make_query.
            handler (Callable[[dict], Deferred[dict]]): Invoked to handle
                incoming queries of this type. The return will be yielded
                on and the result used as the response to the query request.
        """
        if query_type in self.query_handlers:
            raise KeyError(
                "Already have a Query handler for %s" % (query_type,)
            )

        self.query_handlers[query_type] = handler

    @defer.inlineCallbacks
    def on_edu(self, edu_type, origin, content):
        handler = self.edu_handlers.get(edu_type)
        if not handler:
            logger.warn("No handler registered for EDU type %s", edu_type)

        try:
            yield handler(origin, content)
        except SynapseError as e:
            logger.info("Failed to handle edu %r: %r", edu_type, e)
        except Exception as e:
            logger.exception("Failed to handle edu %r", edu_type)

    def on_query(self, query_type, args):
        handler = self.query_handlers.get(query_type)
        if not handler:
            logger.warn("No handler registered for query type %s", query_type)
            raise NotFoundError("No handler for Query type '%s'" % (query_type,))

        return handler(args)
