# ========================================================================== #
#                                                                            #
#    KVMD - The main Pi-KVM daemon.                                          #
#                                                                            #
#    Copyright (C) 2018-2021  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import os
import asyncio
import ipaddress
import dataclasses
import itertools
import argparse

from typing import List
from typing import Optional

from ...logging import get_logger

from ...yamlconf import Section

from ... import env
from ... import aioproc

from .. import init

from .netctl import BaseCtl
from .netctl import IfaceUpCtl
from .netctl import IfaceAddIpCtl
from .netctl import IptablesDropAllCtl
from .netctl import IptablesAllowIcmpCtl
from .netctl import IptablesAllowPortCtl
from .netctl import CustomCtl


# =====
@dataclasses.dataclass(frozen=True)
class _Netcfg:
    iface: str
    iface_ip: str
    net_ip: str
    net_prefix: int
    net_mask: str
    dhcp_ip_begin: str
    dhcp_ip_end: str


class _Service:  # pylint: disable=too-many-instance-attributes
    def __init__(self, config: Section) -> None:
        self.__iface_net: str = config.otgnet.iface.net
        self.__ip_cmd: List[str] = config.otgnet.iface.ip_cmd

        self.__allow_icmp: bool = config.otgnet.firewall.allow_icmp
        self.__allow_tcp: List[int] = sorted(set(config.otgnet.firewall.allow_tcp))
        self.__allow_udp: List[int] = sorted(set(config.otgnet.firewall.allow_udp))
        self.__iptables_cmd: List[str] = config.otgnet.firewall.iptables_cmd

        self.__pre_start_cmd: List[str] = config.otgnet.commands.pre_start_cmd
        self.__post_start_cmd: List[str] = config.otgnet.commands.post_start_cmd
        self.__pre_stop_cmd: List[str] = config.otgnet.commands.pre_stop_cmd
        self.__post_stop_cmd: List[str] = config.otgnet.commands.post_stop_cmd

        self.__gadget: str = config.otg.gadget
        self.__driver: str = config.otg.devices.ethernet.driver

    def start(self) -> None:
        asyncio.run(self.__run(True))

    def stop(self) -> None:
        asyncio.run(self.__run(False))

    async def __run(self, direct: bool) -> None:
        netcfg = self.__make_netcfg()
        placeholders = {
            key: str(value)
            for (key, value) in dataclasses.asdict(netcfg).items()
        }
        ctls: List[BaseCtl] = [
            CustomCtl(self.__pre_start_cmd, self.__post_stop_cmd, placeholders),
            IfaceUpCtl(self.__ip_cmd, netcfg.iface),
            *([IptablesAllowIcmpCtl(self.__iptables_cmd, netcfg.iface)] if self.__allow_icmp else []),
            *[
                IptablesAllowPortCtl(self.__iptables_cmd, netcfg.iface, port, tcp)
                for (port, tcp) in [
                    *zip(self.__allow_tcp, itertools.repeat(True)),
                    *zip(self.__allow_udp, itertools.repeat(False)),
                ]
            ],
            IptablesDropAllCtl(self.__iptables_cmd, netcfg.iface),
            IfaceAddIpCtl(self.__ip_cmd, netcfg.iface, f"{netcfg.iface_ip}/{netcfg.net_prefix}"),
            CustomCtl(self.__post_start_cmd, self.__pre_stop_cmd, placeholders),
        ]
        if direct:
            for ctl in ctls:
                if not (await self.__run_ctl(ctl, True)):
                    raise SystemExit(1)
            get_logger(0).info("Ready to work")
        else:
            for ctl in reversed(ctls):
                await self.__run_ctl(ctl, False)
            get_logger(0).info("Bye-bye")

    async def __run_ctl(self, ctl: BaseCtl, direct: bool) -> bool:
        logger = get_logger()
        cmd = ctl.get_command(direct)
        logger.info("CMD: %s", " ".join(cmd))
        try:
            return (not (await aioproc.log_process(cmd, logger)).returncode)
        except Exception as err:
            logger.exception("Can't execute command: %s", err)
        return False

    # =====

    def __make_netcfg(self) -> _Netcfg:
        iface = self.__find_iface()
        logger = get_logger()

        logger.info("Using IPv4 network %s ...", self.__iface_net)
        net = ipaddress.IPv4Network(self.__iface_net)
        if net.prefixlen > 31:
            raise RuntimeError("Too small network, required at least /31")

        if net.prefixlen == 31:
            iface_ip = str(net[0])
            dhcp_ip_begin = dhcp_ip_end = str(net[1])
        else:
            iface_ip = str(net[1])
            dhcp_ip_begin = str(net[2])
            dhcp_ip_end = str(net[-2])

        netcfg = _Netcfg(
            iface=iface,
            iface_ip=iface_ip,
            net_ip=str(net.network_address),
            net_prefix=net.prefixlen,
            net_mask=str(net.netmask),
            dhcp_ip_begin=dhcp_ip_begin,
            dhcp_ip_end=dhcp_ip_end,
        )
        logger.info("Calculated %r address is %s/%d", iface, iface_ip, netcfg.net_prefix)
        return netcfg

    def __find_iface(self) -> str:
        logger = get_logger()
        path = env.SYSFS_PREFIX + os.path.join(
            "/sys/kernel/config/usb_gadget",
            self.__gadget,
            f"functions/{self.__driver}.usb0/ifname",
        )
        logger.info("Using OTG gadget %r ...", self.__gadget)
        with open(path) as iface_file:
            iface = iface_file.read().strip()
            logger.info("Using OTG Ethernet interface %r ...", iface)
            assert iface
            return iface


# =====
def main(argv: Optional[List[str]]=None) -> None:
    (parent_parser, argv, config) = init(
        add_help=False,
        argv=argv,
    )
    parser = argparse.ArgumentParser(
        prog="kvmd-otgnet",
        description="Control KVMD OTG network",
        parents=[parent_parser],
    )
    parser.set_defaults(cmd=(lambda *_: parser.print_help()))
    subparsers = parser.add_subparsers()

    service = _Service(config)

    cmd_start_parser = subparsers.add_parser("start", help="Start OTG network")
    cmd_start_parser.set_defaults(cmd=service.start)

    cmd_stop_parser = subparsers.add_parser("stop", help="Stop OTG network")
    cmd_stop_parser.set_defaults(cmd=service.stop)

    options = parser.parse_args(argv[1:])
    options.cmd()
