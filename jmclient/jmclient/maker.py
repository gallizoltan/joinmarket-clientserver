#! /usr/bin/env python
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from builtins import * # noqa: F401

import base64
import pprint
import sys
import abc
from binascii import unhexlify


from jmbitcoin import SerializationError, SerializationTruncationError
import jmbitcoin as btc
from jmclient.configure import jm_single
from jmbase.support import get_log
from jmclient.support import (calc_cj_fee)
from jmclient.podle import verify_podle, PoDLE, PoDLEError
from twisted.internet import task
from .cryptoengine import EngineError

jlog = get_log()

class Maker(object):
    def __init__(self, wallet):
        self.active_orders = {}
        self.wallet = wallet
        self.nextoid = -1
        self.offerlist = None
        self.sync_wait_loop = task.LoopingCall(self.try_to_create_my_orders)
        self.sync_wait_loop.start(2.0)
        self.aborted = False

    def try_to_create_my_orders(self):
        """Because wallet syncing is not synchronous(!),
        we cannot calculate our offers until we know the wallet
        contents, so poll until BlockchainInterface.wallet_synced
        is flagged as True. TODO: Use a deferred, probably.
        Note that create_my_orders() is defined by subclasses.
        """
        if not jm_single().bc_interface.wallet_synced:
            return
        self.offerlist = self.create_my_orders()
        self.sync_wait_loop.stop()
        if not self.offerlist:
            jlog.info("Failed to create offers, giving up.")
            sys.exit(0)

    def on_auth_received(self, nick, offer, commitment, cr, amount, kphex):
        """Receives data on proposed transaction offer from daemon, verifies
        commitment, returns necessary data to send ioauth message (utxos etc)
        """
        #check the validity of the proof of discrete log equivalence
        tries = jm_single().config.getint("POLICY", "taker_utxo_retries")
        def reject(msg):
            jlog.info("Counterparty commitment not accepted, reason: " + msg)
            return (False,)

        # deserialize the commitment revelation
        try:
            cr_dict = PoDLE.deserialize_revelation(cr)
        except PoDLEError as e:
            reason = repr(e)
            return reject(reason)

        if not verify_podle(str(cr_dict['P']), str(cr_dict['P2']), str(cr_dict['sig']),
                                str(cr_dict['e']), str(commitment),
                                index_range=range(tries)):
            reason = "verify_podle failed"
            return reject(reason)
        #finally, check that the proffered utxo is real, old enough, large enough,
        #and corresponds to the pubkey
        res = jm_single().bc_interface.query_utxo_set([cr_dict['utxo']],
                                                      includeconf=True)
        if len(res) != 1 or not res[0]:
            reason = "authorizing utxo is not valid"
            return reject(reason)
        age = jm_single().config.getint("POLICY", "taker_utxo_age")
        if res[0]['confirms'] < age:
            reason = "commitment utxo not old enough: " + str(res[0]['confirms'])
            return reject(reason)
        reqd_amt = int(amount * jm_single().config.getint(
            "POLICY", "taker_utxo_amtpercent") / 100.0)
        if res[0]['value'] < reqd_amt:
            reason = "commitment utxo too small: " + str(res[0]['value'])
            return reject(reason)

        try:
            if not self.wallet.pubkey_has_script(
                    unhexlify(cr_dict['P']), unhexlify(res[0]['script'])):
                raise EngineError()
        except EngineError:
            reason = "Invalid podle pubkey: " + str(cr_dict['P'])
            return reject(reason)

        # authorisation of taker passed
        #Find utxos for the transaction now:
        utxos, cj_addr, change_addr = self.oid_to_order(offer, amount)
        if not utxos:
            #could not find funds
            return (False,)
        self.wallet.update_cache_index()
        # Construct data for auth request back to taker.
        # Need to choose an input utxo pubkey to sign with
        # (no longer using the coinjoin pubkey from 0.2.0)
        # Just choose the first utxo in self.utxos and retrieve key from wallet.
        auth_address = utxos[list(utxos.keys())[0]]['address']
        auth_key = self.wallet.get_key_from_addr(auth_address)
        auth_pub = btc.privtopub(auth_key)
        btc_sig = btc.ecdsa_sign(kphex, auth_key)
        return (True, utxos, auth_pub, cj_addr, change_addr, btc_sig)

    def on_tx_received(self, nick, txhex, offerinfo):
        """Called when the counterparty has sent an unsigned
        transaction. Sigs are created and returned if and only
        if the transaction passes verification checks (see
        verify_unsigned_tx()).
        """
        try:
            tx = btc.deserialize(txhex)
        except (IndexError, SerializationError, SerializationTruncationError) as e:
            return (False, 'malformed txhex. ' + repr(e))
        jlog.info('obtained tx\n' + pprint.pformat(tx))
        goodtx, errmsg = self.verify_unsigned_tx(tx, offerinfo)
        if not goodtx:
            jlog.info('not a good tx, reason=' + errmsg)
            return (False, errmsg)
        jlog.info('goodtx')
        sigs = []
        utxos = offerinfo["utxos"]

        our_inputs = {}
        for index, ins in enumerate(tx['ins']):
            utxo = ins['outpoint']['hash'] + ':' + str(ins['outpoint']['index'])
            if utxo not in utxos:
                continue
            script = self.wallet.addr_to_script(utxos[utxo]['address'])
            amount = utxos[utxo]['value']
            our_inputs[index] = (script, amount)

        txs = self.wallet.sign_tx(btc.deserialize(unhexlify(txhex)), our_inputs)
        for index in our_inputs:
            sigmsg = unhexlify(txs['ins'][index]['script'])
            if 'txinwitness' in txs['ins'][index]:
                # Note that this flag only implies that the transaction
                # *as a whole* is using segwit serialization; it doesn't
                # imply that this specific input is segwit type (to be
                # fully general, we allow that even our own wallet's
                # inputs might be of mixed type). So, we catch the EngineError
                # which is thrown by non-segwit types. This way the sigmsg
                # will only contain the scriptSig field if the wallet object
                # decides it's necessary/appropriate for this specific input
                # If it is segwit, we prepend the witness data since we want
                # (sig, pub, witnessprogram=scriptSig - note we could, better,
                # pass scriptCode here, but that is not backwards compatible,
                # as the taker uses this third field and inserts it into the
                # transaction scriptSig), else (non-sw) the !sig message remains
                # unchanged as (sig, pub).
                try:
                    scriptSig = btc.pubkey_to_p2wpkh_script(txs['ins'][index]['txinwitness'][1])
                    sigmsg = b''.join(btc.serialize_script_unit(
                x) for x in txs['ins'][index]['txinwitness'] + [scriptSig])
                except IndexError:
                    #the sigmsg was already set before the segwit check
                    pass
            sigs.append(base64.b64encode(sigmsg).decode('ascii'))
        return (True, sigs)

    def verify_unsigned_tx(self, txd, offerinfo):
        """This code is security-critical.
        Before signing the transaction the Maker must ensure
        that all details are as expected, and most importantly
        that it receives the exact number of coins to expected
        in total. The data is taken from the offerinfo dict and
        compared with the serialized txhex.
        """
        tx_utxo_set = set(ins['outpoint']['hash'] + ':' + str(
                ins['outpoint']['index']) for ins in txd['ins'])

        utxos = offerinfo["utxos"]
        cjaddr = offerinfo["cjaddr"]
        cjaddr_script = btc.address_to_script(cjaddr)
        changeaddr = offerinfo["changeaddr"]
        changeaddr_script = btc.address_to_script(changeaddr)
        #Note: this value is under the control of the Taker,
        #see comment below.
        amount = offerinfo["amount"]
        cjfee = offerinfo["offer"]["cjfee"]
        txfee = offerinfo["offer"]["txfee"]
        ordertype = offerinfo["offer"]["ordertype"]
        my_utxo_set = set(utxos.keys())
        if not tx_utxo_set.issuperset(my_utxo_set):
            return (False, 'my utxos are not contained')

        #The three lines below ensure that the Maker receives
        #back what he puts in, minus his bitcointxfee contribution,
        #plus his expected fee. These values are fully under
        #Maker control so no combination of messages from the Taker
        #can change them.
        #(mathematically: amount + expected_change_value is independent
        #of amount); there is not a (known) way for an attacker to
        #alter the amount (note: !fill resubmissions *overwrite*
        #the active_orders[dict] entry in daemon), but this is an
        #extra layer of safety.
        my_total_in = sum([va['value'] for va in utxos.values()])
        real_cjfee = calc_cj_fee(ordertype, cjfee, amount)
        expected_change_value = (my_total_in - amount - txfee + real_cjfee)
        jlog.info('potentially earned = {}'.format(real_cjfee - txfee))
        jlog.info('mycjaddr, mychange = {}, {}'.format(cjaddr, changeaddr))

        #The remaining checks are needed to ensure
        #that the coinjoin and change addresses occur
        #exactly once with the required amts, in the output.
        times_seen_cj_addr = 0
        times_seen_change_addr = 0
        for outs in txd['outs']:
            if outs['script'] == cjaddr_script:
                times_seen_cj_addr += 1
                if outs['value'] != amount:
                    return (False, 'Wrong cj_amount. I expect ' + str(amount))
            if outs['script'] == changeaddr_script:
                times_seen_change_addr += 1
                if outs['value'] != expected_change_value:
                    return (False, 'wrong change, i expect ' + str(
                        expected_change_value))
        if times_seen_cj_addr != 1 or times_seen_change_addr != 1:
            fmt = ('cj or change addr not in tx '
                   'outputs once, #cjaddr={}, #chaddr={}').format
            return (False, (fmt(times_seen_cj_addr, times_seen_change_addr)))
        return (True, None)

    def modify_orders(self, to_cancel, to_announce):
        """This code is called on unconfirm and confirm callbacks,
        and replaces existing orders with new ones, or just cancels
        old ones.
        """
        jlog.info('modifying orders. to_cancel={}\nto_announce={}'.format(
                to_cancel, to_announce))
        for oid in to_cancel:
            order = [o for o in self.offerlist if o['oid'] == oid]
            if len(order) == 0:
                fmt = 'didnt cancel order which doesnt exist, oid={}'.format
                jlog.info(fmt(oid))
            self.offerlist.remove(order[0])
        if len(to_announce) > 0:
            for ann in to_announce:
                oldorder_s = [o for o in self.offerlist
                              if o['oid'] == ann['oid']]
                if len(oldorder_s) > 0:
                    self.offerlist.remove(oldorder_s[0])
            self.offerlist += to_announce

    def import_new_addresses(self, addr_list):
        # FIXME: same code as in taker.py
        bci = jm_single().bc_interface
        if not hasattr(bci, 'import_addresses'):
            return
        assert hasattr(bci, 'get_wallet_name')
        bci.import_addresses(addr_list, bci.get_wallet_name(self.wallet))

    @abc.abstractmethod
    def create_my_orders(self):
        """Must generate a set of orders to be displayed
        according to the contents of the wallet + some algo.
        (Note: should be called "create_my_offers")
        """

    @abc.abstractmethod
    def oid_to_order(self, cjorder, oid, amount):
        """Must convert an order with an offer/order id
        into a set of utxos to fill the order.
        Also provides the output addresses for the Taker.
        """

    @abc.abstractmethod
    def on_tx_unconfirmed(self, cjorder, txid, removed_utxos):
        """Performs action on receipt of transaction into the
        mempool in the blockchain instance (e.g. announcing orders)
        """

    @abc.abstractmethod
    def on_tx_confirmed(self, cjorder, confirmations, txid):
        """Performs actions on receipt of 1st confirmation of
        a transaction into a block (e.g. announce orders)
        """
