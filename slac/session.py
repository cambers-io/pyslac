import asyncio
import logging
from binascii import hexlify
from dataclasses import dataclass, field
from inspect import isawaitable
from os import urandom
from typing import List, Optional, Union

from slac.enums import (
    ATTEN_RESULTS_TIMEOUT,
    CM_ATTEN_CHAR,
    CM_ATTEN_PROFILE,
    CM_MNBC_SOUND,
    CM_SET_KEY,
    CM_SLAC_MATCH,
    CM_SLAC_PARM,
    CM_START_ATTEN_CHAR,
    ETH_TYPE_HPAV,
    EVSE_ID,
    EVSE_PLC_MAC,
    HOMEPLUG_MMV,
    MMTYPE_CNF,
    MMTYPE_IND,
    MMTYPE_REQ,
    SLAC_ATTEN_TIMEOUT,
    SLAC_GROUPS,
    SLAC_LIMIT,
    SLAC_MSOUNDS,
    SLAC_PAUSE,
    SLAC_RESP_TYPE,
    SLAC_SETTLE_TIME,
    STATE_MATCHED,
    STATE_MATCHING,
    STATE_UNMATCHED,
    FramesSizes,
    Timers,
)

# This timeout is imported from the environment file, because it makes it
# easier to use it with the dev compose file for dev and debugging reasons
from slac.environment import SLAC_INIT_TIMEOUT
from slac.layer_2_headers import EthernetHeader, HomePlugHeader
from slac.messages import (
    AtennChar,
    AtennCharRsp,
    AttenProfile,
    MatchCnf,
    MatchReq,
    MnbcSound,
    SetKeyCnf,
    SetKeyReq,
    SlacParmCnf,
    SlacParmReq,
    StartAtennChar,
)
from slac.sockets.async_linux_socket import (
    create_socket,
    readeth,
    send_recv_eth,
    sendeth,
)
from slac.utils import generate_nid, get_if_hwaddr
from slac.utils import half_round as hw
from slac.utils import time_now_ms

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("slac_session")


@dataclass
class SlacSession:
    # pylint: disable=too-many-instance-attributes
    # State can be from EV or EVSE if we use a common data structure,
    # so its init state must come from the constructor
    state: int

    # 16 bytes
    # Network Mask is a random 16 bytes number
    nmk: bytes = b""

    # 7 bytes NetworkIdentifier
    # The 54 LSBs of this field contain the NID (refer to Section 4.4.3.1).
    # The two MSBs shall be set to 0b00.
    # NID is derived from the Network Mask
    nid: bytes = b""

    # FORWARDING_STA
    # 6 bytes (ETHER_ADDR_LEN) IPV6 ADDRESS for the target to send the
    # SLAC sounds responses to
    # associated with ethernet.OSA

    # EVSE-HLE should copy the message OSA to the session variable PEV
    # MAC address; the PEV MAC address can then be used to respond in
    # unicast to the right PEV-HLE; The PEV-HLE address shall also be
    # included in the FORWARDING_STA field of the SLAC MMEs when
    # addressing other GP (GreenPHY) Station (RESP_TYPE = 1);
    # evse_cm_slac_param.c

    # FORWARDING_STA will be 00:00:00:00:00:00 for SLAC when RESP_TYPE=0;
    # the forwarding stations is a vague concept; the specification
    # authors say it should be FF:FF:FF:FF:FF:FF but here, the EVSE-HLE
    # will set it to the PEV MAC (line 110 of evse_cm_slac_param.c)

    # In evse_cm_slac_param.c docstrings it is also said:
    # The PEVHLE address shall also be
    # included in the FORWARDING_STA field of the SLAC MMEs when
    # addressing other GP STA (RESP_TYPE = 1);

    # According to the spec 15118-3, FORWARDING_STA is the EV Host MAC address
    forwarding_sta: bytes = b""

    # PEV_ID (provided by the EV side and received by EVSE on
    # evse_cm_slac_match)
    # 17 bytes
    pev_id: Optional[int] = None

    # 6 bytes
    # EV MAC address, received by EVSE during evse_cm_slac_param, by getting
    # request->ethernet.OSA
    pev_mac: bytes = b""

    # 17 bytes
    # EVSE_ID
    evse_id: bytes = bytes.fromhex(EVSE_ID)

    # 6 byte EVSE MAC address
    # get channel own MAC in static signed identifier from channel->host
    # received by EV in pev_cm_atten_char for the first time
    evse_mac: bytes = b""

    # 8 bytes identifier used to identify a running session
    # (it is generated by the ev side)
    run_id: bytes = b""
    # 1 byte APPLICATION_TYPE will be 0 for SLAC
    application_type: int = 0x00
    # 1 byte Security Type is also 0x00 for SLAC
    security_type: int = 0x00

    # Counter for the number of CM_START_ATTEN_CHAR.IND received
    # According to ISO15118-3, the EV shall send 3 consecutively
    num_start_attn_rcvd: int = 0

    # Number of total sounds expected to arrive from EV
    # In 15118-3 is associated with CM_EV_match_MNBC
    num_expected_sounds: Optional[int] = None

    # NUM_SOUNDS CM_MNBC_SOUND.IND sent by the EV during SLAC attn charac
    # For the EVSE this variable is incremented during the CM_MNBC_SOUND step
    num_total_sounds: int = 0

    # It is used by the ev (in pev_cm_mnbc_sound.c) to send a determined
    # number of sounds demanded by the evse (SLAC_MSOUNDS).
    sounds: int = SLAC_MSOUNDS

    # Timeout for the reception of Slac sounds
    # The value is 600 ms, but the spec 15118-3 page 38 defines
    # that the value to be transmitted
    # is 0x06, so we divide 600 / 100.
    time_out_ms: int = SLAC_ATTEN_TIMEOUT

    # SLAC_GROUPS = 58 bytes
    # Associated with CM_ATTEN_PROFILE.IND.AAG values defined
    # in evse_cm_mnbc_sound.c
    # if aag: [bytes] = [b'\x00'] * 58
    # an operation with bytes would have to be done like this
    # ag[0] = (int.from_bytes(ag[0], 'big') + 255).to_bytes(1, 'big')
    # So, I guess it is better if it is defined as int from the start and
    # convert to bytes later
    aag: [int] = field(default_factory=lambda: [0] * SLAC_GROUPS)

    # 1byte
    # Number of Slac Groups
    num_groups: Optional[int] = None

    # 17 bytes
    rnd: bytes = (0).to_bytes(17, "big")

    # AttenuationThreshold
    # Limit set in EV side, for checking if it is a match or not
    # 2 or 4 bytes? depends on the architecture
    slac_threshold: int = SLAC_LIMIT

    # This value is used when the ev is sending sound signals to the EVSE.
    # A brief delay (TP_EV_batch_msg_interval) of a few milliseconds is needed
    # between msounds so that EVSE - PLC has time to forward CM_MNBC_SOUND.IND
    # and CM_ATTEN_PROFILE.IND to EVSE-HLE
    pause: int = SLAC_PAUSE

    # Time used by the EVSE and EV to wait after sending a CM_SET_KEY.REQ
    settle_time: int = SLAC_SETTLE_TIME

    # contains the reference to the task running the matching session
    matching_process_task: Optional[asyncio.Task] = None

    # counter for the id of the MQTT API
    mqtt_msg_counter: int = 0

    def reset(self):
        """
        It resets the session values to their default values
        NID and NMK are not reset, because who handles the operation is the
        call to evse_set_key: as defined by the standard, if we cant set a new
        NID and NMK, then we shall use the already defined ones
        """
        self.state = STATE_UNMATCHED
        self.forwarding_sta = b""
        self.pev_id = None
        self.pev_mac = b""
        self.evse_id = bytes.fromhex(EVSE_ID)
        self.evse_mac = b""
        self.run_id = b""
        self.application_type = 0x00
        self.security_type = 0x00
        self.num_start_attn_rcvd = 0
        self.num_expected_sounds = None
        self.num_total_sounds = 0
        self.sounds = SLAC_MSOUNDS
        self.time_out_ms = SLAC_ATTEN_TIMEOUT
        self.aag = field(default_factory=lambda: [0] * SLAC_GROUPS)
        self.num_groups = None
        self.rnd = (0).to_bytes(17, "big")
        self.slac_threshold = SLAC_LIMIT
        self.pause = SLAC_PAUSE
        self.settle_time = SLAC_SETTLE_TIME
        self.matching_process_task = None


class SlacEvseSession(SlacSession):
    # pylint: disable=too-many-instance-attributes, too-many-arguments
    # pylint: disable=logging-fstring-interpolation, broad-except
    def __init__(self, iface: str):

        host_mac = get_if_hwaddr(iface)
        self.iface = iface
        self.socket = create_socket(iface=self.iface, port=0)
        self.evse_plc_mac = EVSE_PLC_MAC
        SlacSession.__init__(self, state=STATE_UNMATCHED, evse_mac=host_mac)

    def reset_socket(self):
        self.socket.close()
        self.socket = create_socket(iface=self.iface, port=0)

    async def send_frame(self, frame_to_send: bytes) -> None:
        """
        Async wrapper for a sendeth that checks if sendeth is an awaitable
        """
        # TODO: Add this to a send method
        bytes_sent = sendeth(
            s=self.socket, frame_to_send=frame_to_send, iface=self.iface
        )
        if isawaitable(bytes_sent):
            await bytes_sent

    async def rcv_frame(self, rcv_frame_size: int, timeout: Union[float, int]) -> bytes:
        """
        Helper function to diminush the lines of code when calling the
        asyncio.wait_for with readeth

        :param rcv_frame_size: size of the frame to be received
        :param timeout: timeout for the specific message that is being expected
        :return:
        """
        return await asyncio.wait_for(
            readeth(self.socket, self.iface, rcv_frame_size), timeout
        )

    async def leave_logical_network(self):
        """
        As defined by ISO15118-3 section 9.6, requirement [V2G3-M09-17],
        when leaving the logical network, the parameters associated with
        the current session must be reset to their default values and the
        state of the session shall now be "Unmatched"
        As preparation for the next session, we set a new NMK and NID
        """
        await self.evse_set_key()
        self.reset()

    async def evse_set_key(self):
        """
        PEV-HLE sets the NMK and NID on PEV-PLC using CM_SET_KEY.REQ;
        the NMK and NID must match those provided by EVSE-HLE using
        CM_SLAC_MATCH.CNF;

        The configuration of the low-layer communication module with the
        parameters of the logical network may be done with the MMEs
        CM_SET_KEY.REQ and CM_SET_KEY.CNF.
        Table A.8 from ISO15118-3 defines all the parameters needed and their
        value for this call

        My Nonce for that STA may remain constant for a run of a protocol),
        but a new nonce should be generated for each new protocol run.
        This reflects the purpose of nonces to provide a STA with a quantity i
        t believes to be freshly generated (to defeat replay attacks) and to
        use for association of messages within the protocol run.
        Refer to Section 7.10.7.3 for generation of nonces.

        The only secure way to remove a STA from an AVLN is to change the NMK
        """
        logger.debug("CM_SET_KEY: Started...")
        # for each new set_key message sent (or slac session),
        # a new pair of NID (Network ID) and NMK (Network Mask) shall be
        # generated
        nmk = urandom(16)
        # the NID shall be derived from the NMK and its 2 MSBs must be 0b00
        nid = generate_nid(nmk)
        logger.debug("New NMK: %s\n", hexlify(nmk))
        logger.debug("New NID: %s\n", hexlify(nid))
        ethernet_header = EthernetHeader(
            dst_mac=self.evse_plc_mac, src_mac=self.evse_mac
        )
        homeplug_header = HomePlugHeader(CM_SET_KEY | MMTYPE_REQ)
        key_req_payload = SetKeyReq(nid=nid, new_key=nmk)

        frame_to_send = (
            ethernet_header.pack_big()
            + homeplug_header.pack_big()
            + key_req_payload.pack_big()
        )

        # TODO: Change this to just open a socket once for every SlacSession
        # and not every time we call send or send_recv_eth
        # Also think about including the send, rcv method as inner methods of
        # SetKeyReq. Maybe even create a class SetKey that handles both the
        # Send and the CNF of the message
        payload_rcvd = send_recv_eth(
            frame_to_send=frame_to_send,
            rcv_frame_size=FramesSizes.CM_SET_KEY_CNF,
            iface=self.iface,
        )
        if isawaitable(payload_rcvd):
            payload_rcvd = await payload_rcvd

        try:
            SetKeyCnf.from_bytes(payload_rcvd)
            self.nmk = nmk
            self.nid = nid
        except ValueError as e:
            logger.error(e)
            logger.debug(
                "SetKeyReq has failed, old NMK: %s and NID: %s apply",
                self.nmk,
                self.nid,
            )
            return payload_rcvd
        await asyncio.sleep(SLAC_SETTLE_TIME)
        logger.debug("CM_SET_KEY: Finished!")
        return payload_rcvd

    async def evse_slac_parm(self) -> None:
        logger.debug("CM_SLAC_PARM: Started...")
        # TODO: Pass the expected parameters later to the read function
        # so that it can be evaluated while the timeout hasnt elapsed
        self.reset_socket()
        try:
            # A complete CM_SLAC_PARM.REQ frame must have 60 Bytes:
            # EthernetHeader = 14 bytes
            # HomePlugHeader  = 5 bytes
            # SlacParmReq = 10 bytes
            # Padding = 31 bytes (The min ETH frame must have 60 bytes,
            # it this frame requires padding)
            data_rcvd = await self.rcv_frame(
                rcv_frame_size=FramesSizes.CM_SLAC_PARM_REQ, timeout=SLAC_INIT_TIMEOUT
            )
        except TimeoutError as e:
            self.state = STATE_UNMATCHED
            logger.warning(f"Timeout waiting for CM_SLAC_PARM.REQ: {e}")
            return
        try:
            ether_frame = EthernetHeader.from_bytes(data_rcvd)
            homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
            slac_parm_req = SlacParmReq.from_bytes(data_rcvd)
        except Exception as e:
            self.state = STATE_UNMATCHED
            # TODO: PROPER Exception
            logger.exception(e, exc_info=True)
            return
        if homeplug_frame.mm_type != CM_SLAC_PARM | MMTYPE_REQ:
            logger.info(
                f"MMTYPE {homeplug_frame.mm_type} does not correspond"
                f"to the expected one CM_SLAC_PARM | MMTYPE_REQ"
            )
            self.state = STATE_UNMATCHED
            return

        # Saving SLAC_PARM_REQ parameters from EV
        self.application_type = slac_parm_req.application_type
        self.security_type = slac_parm_req.security_type
        self.run_id = slac_parm_req.run_id

        # both fields are filled with the EV MAC Address
        self.pev_mac = ether_frame.src_mac
        self.forwarding_sta = ether_frame.src_mac

        # SLAC_PARM_CNF frame formation
        ether_header = EthernetHeader(dst_mac=self.pev_mac, src_mac=self.evse_mac)
        homeplug_header = HomePlugHeader(CM_SLAC_PARM | MMTYPE_CNF)
        slac_parm_cnf = SlacParmCnf(forwarding_sta=self.pev_mac, run_id=self.run_id)

        frame_to_send = (
            ether_header.pack_big()
            + homeplug_header.pack_big()
            + slac_parm_cnf.pack_big()
        )

        await self.send_frame(frame_to_send)
        logger.debug("Sent SLAC_PARM.CNF")

        # Update SLAC Session State, indicating that is occupied and ready for
        # a match decision process
        self.state = STATE_MATCHING

        logger.debug("CM_SLAC_PARM: Finished!")

    async def cm_start_atten_charac(self):
        logger.debug("CM_START_ATTEN_CHAR: Started...")
        try:
            # A complete CM_START_ATTEN_CHAR.IND frame must have 60 Bytes:
            # EthernetHeader = 14 bytes
            # HomePlugHeader  = 5 bytes
            # StartAtennChar = 19 bytes
            # Padding = 22 bytes (The min ETH frame must have 60 bytes,
            # it this frame requires padding)
            data_rcvd = await self.rcv_frame(
                rcv_frame_size=FramesSizes.CM_START_ATTEN_CHAR_IND,
                timeout=Timers.SLAC_REQ_TIMEOUT,
            )
            EthernetHeader.from_bytes(data_rcvd)
            homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
            start_atten_char = StartAtennChar.from_bytes(data_rcvd)
        except Exception as e:
            self.state = STATE_UNMATCHED
            logger.exception(e, exc_info=True)
            return
        if (
            self.application_type != start_atten_char.application_type
            or self.security_type != start_atten_char.security_type
            or self.run_id != start_atten_char.run_id
            or start_atten_char.resp_type != SLAC_RESP_TYPE
            or homeplug_frame.mm_type != CM_START_ATTEN_CHAR | MMTYPE_IND
        ):
            self.state = STATE_UNMATCHED
            logger.exception(ValueError("Error in StartAttenChar"))
            return
            # raise ValueError("Error in StartAttenChar")

        # As is stated in ISO15118-3, the EV will send 3 consecutive
        # CM_START_ATTEN_CHAR, regardless if the first one was correctly
        # received and processed. However, the PLC just forwards 1 to the
        # application, so we just need to process 1

        # Saving START_ATTEN_CHAR parameters from EV
        self.num_expected_sounds = start_atten_char.num_sounds
        # the value sent by the EV for the timeout has a factor of 1/100
        # Thus, if the value is e.g. 6, the original value is 600 ms (6 * 100)
        # ATTENTION: Given that Alfen Socket Board interaction with the
        # Smart Controller Board adds a big overhead when sending the data
        # frames, SLAC does not receive all the sounds within
        # `start_atten_char.time_out`. However, according to the following:
        # [V2G3-A09-30] - The EV shall start the timeout timer
        # TT_EV_atten_results (max 1200 ms) when sending the first
        # CM_START_ATTEN_CHAR.IND.
        # [V2G3-A09-31] - While the timer TT_EV_atten_results (max 1200 ms) is
        # running, the EV shall process incoming CM_ATTEN_CHAR.IND messages.
        # Which means, we can use a larger timeout (like 900 ms) so that
        # we receive all or mostly all of the sounds.
        # self.time_out_ms = start_atten_char.time_out * 100
        self.time_out_ms = ATTEN_RESULTS_TIMEOUT * 100
        self.forwarding_sta = start_atten_char.forwarding_sta
        logger.debug("CM_START_ATTEN_CHAR: Finished!")

    def process_sound_frame(
        self,
        homeplug_frame: "HomePlugHeader",
        ether_frame: "EthernetHeader",
        data_rcvd: bytes,
        sounds_rcvd: int,
        aag: List[int],
    ) -> int:
        """
        Helper function that checks which kind of frame was received
        and properly updates the number of sounds received during
        the cm_sounds_loop loop

        returns the next frame size expected
        """
        if homeplug_frame.mm_type == CM_MNBC_SOUND | MMTYPE_IND:
            mnbc_sound_ind = MnbcSound.from_bytes(data_rcvd)
            if self.run_id == mnbc_sound_ind.run_id:
                if self.pev_mac != ether_frame.src_mac:
                    # TODO: Raise Proper Exception
                    raise ValueError(
                        f"Unexpected Source MAC Address for sound "
                        f"number {sounds_rcvd}. "
                        f"PEV MAC: {self.pev_mac}; "
                        f"Source MAC: {ether_frame.src_mac}"
                    )
                logger.debug("MNBC Sound received\n")
                logger.debug("Remaining number of sounds: %s", mnbc_sound_ind.cnt)
            else:
                logger.debug(
                    "Frame received is a CM_MNBC_SOUND but "
                    "it has an invalid Running Session ID. "
                    "Session RunID: %s\n Received RunID: %s",
                    self.run_id,
                    mnbc_sound_ind.run_id,
                )
            return FramesSizes.CM_ATTEN_PROFILE_IND

        if homeplug_frame.mm_type == CM_ATTEN_PROFILE | MMTYPE_IND:
            atten_profile_ind = AttenProfile.from_bytes(data_rcvd)
            if self.pev_mac == atten_profile_ind.pev_mac:
                # Summation of all sounds received per group
                for group in range(atten_profile_ind.num_groups):
                    aag[group] += atten_profile_ind.aag[group]
                self.num_groups = atten_profile_ind.num_groups
                self.num_total_sounds += 1
                logger.debug("ATTEN_Profile Sounds received %s", self.num_total_sounds)
                logger.debug(
                    "Num total sounds: %s / Num expected: %s",
                    self.num_total_sounds,
                    self.num_expected_sounds,
                )
            else:
                logger.warning(
                    "PEV MAC %s does not match: %s. Ignoring...",
                    self.pev_mac,
                    atten_profile_ind.pev_mac,
                )
            return FramesSizes.CM_MNBC_SOUND_IND

    async def cm_sounds_loop(self):
        """
        The GP specification recommends that the EVSE-HLE set an overall
        timer once the cm_start_atten_char message is received and use it
        to terminate the msound loop in case some msounds are lost

        During this process, the EV will send a CM_MNBC_SOUND.IND containing
        a payload that corresponds and is defined within the class MnbcSound as:
        |Application Type|Security Type|SenderID|Cnt|RunID|RSVD|Rnd|

        For each CM_MNBC_SOUND.IND, the EVSE PLC node will send to the host
        application a CM_ATTEN_PROFILE.IND whose payload is defined within
        the class AttenProfile:
        |PEV MAC|NumGroups|RSVD|AAG 1| AAG 2| AAG 3...|

        The sounds reception loop is comprised by the following steps:
        1. awaiting for the reception of a packet
        2. Check for incorrect metadata like Application Type, RunID, ...
        3. Check if the packet is a CM_MNBC_SOUND or CM_ATTEN_PROFILE
        4. if it is a CM_MNBC_SOUND


        accept only CM_MNBC_SOUND.IND that match RunID from the earlier
        CM_SLAC_PARAM.REQ and CM_START_ATTRN_CHAT.IND;

        each CM_MNBC_MSOUND.IND is accompanied by a CM_ATTEN_PROFILE.IND
        but sometimes they arrive out of expected order;

        store the running total of CM_ATTEN_PROFILE.IND.AAG values in
        the session variable and compute the average based on actual
        number of sounds before returning;
        """
        logger.debug("CM_MNBC_SOUND: Started...")
        sounds_rcvd: int = 0
        aag: List[int] = [0] * SLAC_GROUPS
        self.aag = [0] * SLAC_GROUPS
        # time stamp of the start of the signal attenuation measurement and calc
        time_start = time_now_ms()
        self.num_total_sounds = 0
        # We receive in an alternated way the messages
        # CM_MNBC_SOUND.IND and CM_ATTEN_PROFILE.IND and the first
        # message is always a CM_MNBC_SOUND.IND
        next_frame_expected: int = FramesSizes.CM_MNBC_SOUND_IND
        while True:
            try:
                data_rcvd = await self.rcv_frame(
                    rcv_frame_size=next_frame_expected,
                    # The SLAC_REQ_TIMEOUT used seems to not be enough for the
                    # PLC chip to send a sound, so we use 1 sec instead
                    timeout=1,
                )
                ether_frame = EthernetHeader.from_bytes(data_rcvd)
                homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
            except Exception as e:
                self.state = STATE_UNMATCHED
                logger.exception(e, exc_info=True)
                return
                # raise TimeoutError(
                #     "SLAC_TIMEOUT Expired; AttnChar Failed") from e
            if (
                ether_frame.ether_type == ETH_TYPE_HPAV
                and homeplug_frame.mmv == HOMEPLUG_MMV
            ):
                next_frame_expected = self.process_sound_frame(
                    homeplug_frame, ether_frame, data_rcvd, sounds_rcvd, aag
                )

                # Check for a timeout of a reception of the expected sounds
                time_elapsed = time_now_ms() - time_start
                if (
                    time_elapsed < self.time_out_ms
                    and self.num_total_sounds < self.num_expected_sounds
                ):
                    continue

                # Time specified by the EV for the Characterization has expired
                # or num of total sounds is >= expected sounds thus, the Atten
                # data must be grouped and averaged before the loop is
                # terminated [V2G3-A09-19]
                if self.num_total_sounds > 0:
                    for group in range(SLAC_GROUPS):
                        self.aag[group] = hw(aag[group] / self.num_total_sounds)
                logger.debug("CM_MNBC_SOUND: Finished!")
                return

    async def cm_atten_char(self):
        logger.debug("CM_ATTEN_CHAR Started...")
        ether_header = EthernetHeader(dst_mac=self.pev_mac, src_mac=self.evse_mac)
        homeplug_header = HomePlugHeader(CM_ATTEN_CHAR | MMTYPE_IND)
        atten_charac = AtennChar(
            source_address=self.pev_mac,
            run_id=self.run_id,
            num_sounds=self.num_total_sounds,
            num_groups=self.num_groups,
            aag=self.aag,
        )

        frame_to_send = (
            ether_header.pack_big()
            + homeplug_header.pack_big()
            + atten_charac.pack_big()
        )

        await self.send_frame(frame_to_send)
        try:
            # A complete CM_ATTEN_CHAR.RSP frame must have 70 Bytes:
            # EthernetHeader = 14 bytes
            # HomePlugHeader  = 5 bytes
            # AttenCharRsp = 51 bytes
            data_rcvd = await self.rcv_frame(
                rcv_frame_size=FramesSizes.CM_ATTEN_CHAR_RSP,
                # The SLAC_RESP_TIMEOUT used seems to not be enough for the
                # PLC chip to send a sound, so we use 1 sec instead
                timeout=1,
            )
            logger.debug(f"Payload Received: \n {hexlify(data_rcvd)}")
            ether_frame = EthernetHeader.from_bytes(data_rcvd)
            homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
            atten_charac_response = AtennCharRsp.from_bytes(data_rcvd)
        except Exception as e:
            self.state = STATE_UNMATCHED
            logger.exception(e, exc_info=True)
            return
            # raise TimeoutError("SLAC_TIMEOUT Expired; AttnChar Failed") from e

        if (
            ether_frame.ether_type != ETH_TYPE_HPAV
            or homeplug_frame.mmv != HOMEPLUG_MMV
            or self.run_id != atten_charac_response.run_id
        ):
            # TODO: add __str__ or __repr__ methods to the classes
            # for a neat printing
            logger.exception(ether_frame)
            logger.exception(homeplug_frame)
            logger.exception(atten_charac_response)
            # TODO: Check if we shall raise an Error or just ignore
            # According with [V2G3-A09-47] from ISO15118-3, it shall just be
            # ignored
            e = ValueError(
                "AttenChar Resp Failed, ether type or homeplug " "frame are incorrect."
            )
            logger.exception(e)
            self.state = STATE_UNMATCHED
            # raise ValueError("AttenChar Resp Failed, ether type or homeplug "
            #                  "frame are incorrect.")
        if atten_charac_response.result != 0:
            self.state = STATE_UNMATCHED
            e = ValueError("Atten Char Resp Failed: Atten Char Result " "is not 0x00")
            logger.exception(e)
            return
            # raise ValueError("Atten Char Resp Failed: Atten Char Result "
            #                  "is not 0x00")
        logger.debug("CM_ATTEN_CHAR: Finished!")

    async def cm_slac_match(self):
        logger.debug("CM_SLAC_MATCH: Started...")
        # Await for a CM_SLAC_MATCH.REQ from EV
        try:
            # A complete CM_SLAC_MATCH.REQ frame must have 85 Bytes:
            # EthernetHeader = 14 bytes
            # HomePlugHeader  = 5 bytes
            # AttenCharRsp = 66 bytes
            data_rcvd = await self.rcv_frame(
                rcv_frame_size=FramesSizes.CM_SLAC_MATCH_REQ,
                timeout=Timers.SLAC_MATCH_TIMEOUT,
            )

            logger.debug(f"Payload Received: \n {hexlify(data_rcvd)}")
            ether_frame = EthernetHeader.from_bytes(data_rcvd)
            homeplug_frame = HomePlugHeader.from_bytes(data_rcvd)
            slac_match_req = MatchReq.from_bytes(data_rcvd)
        except Exception as e:
            logger.exception(e, exc_info=True)
            self.state = STATE_UNMATCHED
            return
            # raise ValueError("SLAC Match Failed") from e

        if (
            ether_frame.ether_type != ETH_TYPE_HPAV
            or homeplug_frame.mmv != HOMEPLUG_MMV
            or homeplug_frame.mm_type != CM_SLAC_MATCH | MMTYPE_REQ
            or slac_match_req.run_id != self.run_id
        ):
            # TODO: add __str__ or __repr__ methods to the classes
            # for a neat printing
            logger.debug(
                f"ether_type: {ether_frame.ether_type} \n" f"Expected: {ETH_TYPE_HPAV}"
            )
            logger.debug(f"MMV: {homeplug_frame.mmv} \n " f"Expected: {HOMEPLUG_MMV}")
            logger.debug(
                f"MMType: {homeplug_frame.mm_type} \n "
                f"Expected: {CM_SLAC_MATCH | MMTYPE_REQ}"
            )
            logger.debug(
                f"RunId: {slac_match_req.run_id} \n " f"Expected: {self.run_id}"
            )
            # TODO: Check if we shall raise an Error or just ignore
            # according with requirement [V2G3-A09-98] from ISO15118-3
            # it shall be ignored
            raise ValueError("SLAC Match Request Failed")

        self.pev_id = slac_match_req.pev_id
        self.pev_mac = slac_match_req.pev_mac

        # Send Slac Match Confirmation Message
        ether_header = EthernetHeader(dst_mac=self.pev_mac, src_mac=self.evse_mac)
        homeplug_header = HomePlugHeader(CM_SLAC_MATCH | MMTYPE_CNF)
        slac_match_conf = MatchCnf(
            pev_mac=self.pev_mac,
            evse_mac=self.evse_mac,
            run_id=self.run_id,
            nid=self.nid,
            nmk=self.nmk,
        )

        frame_to_send = (
            ether_header.pack_big()
            + homeplug_header.pack_big()
            + slac_match_conf.pack_big()
        )

        await self.send_frame(frame_to_send)
        logger.debug("CM_SLAC_MATCH: Finished!")
        self.state = STATE_MATCHED

    async def is_link_status_active(self) -> bool:
        """
        This is something I checked that Intec does
        They send a HPGP message called LINK_STATUS.REQ to check if the
        the link between the PEV and EVSE is healthy

        The call is done each 0.5s after the send of a CM_SLAC_MATCH.CNF
        In order to not stress out the chip with requests, we do every 2 secs
        """
        logger.debug("Checking Link Status: Started...")
        ethernet_header = EthernetHeader(
            dst_mac=self.evse_plc_mac, src_mac=self.evse_mac
        )
        LINK_STATUS = 0xA0B8
        mmv = b"\x00"
        mm_type = LINK_STATUS | MMTYPE_REQ
        # Link Status Req does not use the fragmentation fields
        # TODO: Add an option in HomeplgugHeader to add this use case
        homeplug_header_no_fragm = mmv + mm_type.to_bytes(2, "little")
        vendor_mme = 0x00B052
        link_status_req_payload = vendor_mme.to_bytes(3, "big")

        frame_to_send = (
            ethernet_header.pack_big()
            + homeplug_header_no_fragm
            + link_status_req_payload
        )

        # A complete LINK_STATUS.CNF frame must have 60 Bytes:
        # EthernetHeader = 14 bytes
        # HomePlugHeaderNoFrag  = 3 bytes
        # LinkStatusRsp = 3 bytes
        # Padding = 40 bytes (The min ETH frame must have 60 bytes,
        # it this frame requires padding)
        payload_rcvd = send_recv_eth(
            frame_to_send=frame_to_send,
            s=self.socket,
            iface=self.iface,
            rcv_frame_size=FramesSizes.LINK_STATUS_CNF,
        )
        if isawaitable(payload_rcvd):
            payload_rcvd = await payload_rcvd

        logger.debug(f"Payload Received {payload_rcvd}")
        try:
            # TODO: Create the Link Status Class and HomePlug to get properly
            # TODO: the info
            mm_type_rcvd = int.from_bytes(payload_rcvd[15:17], "little")
            if mm_type_rcvd != (LINK_STATUS | MMTYPE_CNF):
                raise ValueError("Message received is not LINK_STATUS.CNF")
        except ValueError as e:
            logger.error(e)
            logger.debug("Link Status: Error")
            return False
        logger.debug("Link Status: Active")
        return True

    async def atten_charac_routine(self):
        await self.cm_start_atten_charac()
        await self.cm_sounds_loop()
        await self.cm_atten_char()
        await self.cm_slac_match()
