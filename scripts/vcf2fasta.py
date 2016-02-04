#!/usr/bin/env python
'''
Merge SNP data from multiple VCF files into a single fasta file.

Created on 5 Oct 2015

@author: alex
'''
import argparse
from collections import OrderedDict
from collections import defaultdict
import glob
import itertools
import logging
import os

from Bio import SeqIO
from bintrees import FastRBTree
import vcf

from phe.variant_filters import IUPAC_CODES
from phe.vcf import PHEVCFLightReader


# Try importing the matplotlib and numpy for stats.
try:
    from matplotlib import pyplot as plt
    import numpy
    can_stats = True
except ImportError:
    can_stats = False



class base_stats(object):
    def __init__(self):
        self.N = 0
        self.mut = 0
        self.gap = 0
        self.mix = 0
        self.total = 0

    def __str__(self):
        return "N: %i, mut: %i, mix: %i, gap: %i, total: %i" % (self.N, self.mut, self.mix, self.gap, self.total)

def plot_stats(pos_stats, total_samples, plots_dir="plots", discarded={}):
    if not os.path.exists(plots_dir):
        os.makedirs(plots_dir)

    for contig in pos_stats:
        plt.style.use('ggplot')

        x = numpy.array([pos for pos in pos_stats[contig] if pos not in discarded.get(contig, [])])
        y = numpy.array([ float(pos_stats[contig][pos]["stats"].mut) / total_samples for pos in pos_stats[contig] if pos not in discarded.get(contig, []) ])

        f, (ax1, ax2, ax3, ax4) = plt.subplots(4, sharex=True, sharey=True)
        f.set_size_inches(12, 15)
        ax1.plot(x, y, 'ro')
        ax1.set_title("Fraction of samples with SNPs")
        plt.ylim(0, 1.1)

        y = numpy.array([ float(pos_stats[contig][pos]["stats"].N) / total_samples for pos in pos_stats[contig] if pos not in discarded.get(contig, [])])
        ax2.plot(x, y, 'bo')
        ax2.set_title("Fraction of samples with Ns")

        y = numpy.array([ float(pos_stats[contig][pos]["stats"].mix) / total_samples for pos in pos_stats[contig] if pos not in discarded.get(contig, [])])
        ax3.plot(x, y, 'go')
        ax3.set_title("Fraction of samples with mixed bases")

        y = numpy.array([ float(pos_stats[contig][pos]["stats"].gap) / total_samples for pos in pos_stats[contig] if pos not in discarded.get(contig, [])])
        ax4.plot(x, y, 'yo')
        ax4.set_title("Fraction of samples with uncallable genotype (gap)")

        contig = contig.replace("/", "-")
        plt.savefig(os.path.join(plots_dir, "%s.png" % contig), dpi=100)

def get_mixture(record, threshold):
    mixtures = {}
    try:
        if len(record.samples[0].data.AD) > 1:

            total_depth = sum(record.samples[0].data.AD)
            # Go over all combinations of touples.
            for comb in itertools.combinations(range(0, len(record.samples[0].data.AD)), 2):
                i = comb[0]
                j = comb[1]

                alleles = list()

                if 0 in comb:
                    alleles.append(str(record.REF))

                if i != 0:
                    alleles.append(str(record.ALT[i - 1]))
                    mixture = record.samples[0].data.AD[i]
                if j != 0:
                    alleles.append(str(record.ALT[j - 1]))
                    mixture = record.samples[0].data.AD[j]

                ratio = float(mixture) / total_depth
                if ratio == 1.0:
                    logging.debug("This is only designed for mixtures! %s %s %s %s", record, ratio, record.samples[0].data.AD, record.FILTER)

                    if ratio not in mixtures:
                        mixtures[ratio] = []
                    mixtures[ratio].append(alleles.pop())

                elif ratio >= threshold:
                    try:
                        code = IUPAC_CODES[frozenset(alleles)]
                        if ratio not in mixtures:
                            mixtures[ratio] = []
                            mixtures[ratio].append(code)
                    except KeyError:
                        logging.warn("Could not retrieve IUPAC code for %s from %s", alleles, record)
    except AttributeError:
        mixtures = {}

    return mixtures

def print_stats(stats, pos_stats, total_vars):
    for contig in stats:
        for sample, info in stats[contig].items():
            print "%s,%i,%i" % (sample, len(info.get("n_pos", [])), total_vars)

    for contig in stats:
        for pos, info in pos_stats[contig].iteritems():
            print "%s,%i,%i,%i,%i" % (contig, pos, info.get("N", "NA"), info.get("-", "NA"), info.get("mut", "NA"))


def get_args():
    args = argparse.ArgumentParser(description="Combine multiple VCFs into a single FASTA file.")

    group = args.add_mutually_exclusive_group(required=True)
    group.add_argument("--directory", "-d", help="Path to the directory with .vcf files.")
    group.add_argument("--input", "-i", type=str, nargs='+', help="List of VCF files to process.")

    args.add_argument("--regexp", type=str, help="Regular expression for finding VCFs in a directory.")

    args.add_argument("--out", "-o", required=True, help="Path to the output FASTA file.")

    args.add_argument("--with-mixtures", type=float, help="Specify this option with a threshold to output mixtures above this threshold.")

    args.add_argument("--column-Ns", type=float, help="Keeps columns with fraction of Ns above specified threshold.")
    args.add_argument("--column-gaps", type=float, help="Keeps columns with fraction of Ns above specified threshold.")

    args.add_argument("--sample-Ns", type=float, help="Keeps samples with fraction of Ns above specified threshold.")

    args.add_argument("--reference", type=str, help="If path to reference specified (FASTA), then whole genome will be written.")

    group = args.add_mutually_exclusive_group()

    group.add_argument("--include")
    group.add_argument("--exclude")

    args.add_argument("--with-stats", help="If a path is specified, then position of the outputed SNPs is stored in this file. Requires mumpy and matplotlib.")
    args.add_argument("--plots-dir", default="plots", help="Where to write summary plots on SNPs extracted. Requires mumpy and matplotlib.")

    args.add_argument("--debug", action="store_true", help="More verbose logging (default: turned off).")
    args.add_argument("--local", action="store_true", help="Re-read the VCF instead of storing it in memory.")

    return args.parse_args()

def main():
    """
    Process VCF files and merge them into a single fasta file.
    """
    args = get_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    contigs = list()

    sample_stats = dict()
    samples = list()

    # All positions available for analysis.
    avail_pos = dict()
    # Stats about each position in each chromosome.
    pos_stats = dict()
    indel_summary = defaultdict(int)
    # Cached version of the data.
    vcf_data = dict()
    mixtures = dict()

    empty_tree = FastRBTree()

    exclude = {}
    include = {}

    blank_stat = {"N": 0, "-": 0, "mut": 0, "mix": 0, "gap": 0}

    if args.reference:
        ref_seq = OrderedDict()
        with open(args.reference) as fp:
            for record in SeqIO.parse(fp, "fasta"):
                ref_seq[record.id] = str(record.seq)

        args.reference = ref_seq

    if args.exclude or args.include:
        pos = {}
        chr_pos = []
        bed_file = args.include if args.include is not None else args.exclude

        with open(bed_file) as fp:
            for line in fp:
                data = line.strip().split("\t")

                chr_pos += [ (i, False,) for i in xrange(int(data[1]), int(data[2]) + 1)]

                if data[0] not in pos:
                    pos[data[0]] = []

                pos[data[0]] += chr_pos


        pos = {chrom: FastRBTree(l) for chrom, l in pos.items()}

        if args.include:
            include = pos
        else:
            exclude = pos


    if args.directory is not None and args.input is None:
        regexp = args.regexp if args.regexp else "*.vcf"
        args.input = glob.glob(os.path.join(args.directory, regexp))
    else:
        args.input = [args.input]

    if not args.input:
        logging.warn("No VCFs found.")
        return 0

    # First pass to get the references and the positions to be analysed.
    for vcf_in in args.input:
        sample_name, _ = os.path.splitext(os.path.basename(vcf_in))
        samples.append(sample_name)
        sample_stats[sample_name] = base_stats()


        reader = PHEVCFLightReader(filename=vcf_in)

        for record in reader:

            if include.get(record.CHROM, empty_tree).get(record.POS, False) or \
                not exclude.get(record.CHROM, empty_tree).get(record.POS, True):
                continue

            if record.CHROM not in contigs:
                contigs.append(record.CHROM)
                avail_pos[record.CHROM] = FastRBTree()

            if avail_pos[record.CHROM].get(record.POS, None) is None:
                avail_pos[record.CHROM].insert(record.POS, {"reference": str(record.REF),
                                                            "stats": base_stats()})


            position_data = avail_pos[record.CHROM].get(record.POS)

            if record.samples[0].data.GT in ("./.", None) :
                position_data[sample_name] = "-"
                position_data["stats"].gap += 1
                sample_stats[sample_name].gap += 1
            elif not record.FILTER:
                # Make sure the reference base is the same. Maybe a vcf from different species snuck in here?!
                assert str(record.REF) == position_data["reference"], "SOMETHING IS REALLY WRONG because reference for the same position is DIFFERENT! %s in %s" % (record.POS, vcf_in)

                if record.is_snp:
                    if len(record.ALT) > 1:
                        logging.info("POS %s passed filters but has multiple alleles. Inserting N")
                        position_data[sample_name] = "N"

                        position_data["stats"].N += 1
                        sample_stats[sample_name].N += 1
                    else:
                        position_data[sample_name] = str(record.ALT[0])

                        position_data["stats"].mut += 1
                        sample_stats[sample_name].mut += 1


            elif record.is_snp:
                # mix = get_mixture(record, args.with_mixtures)
                # Currently we are only using first filter to call consensus.
                extended_code = "N"

                if extended_code == "N":
                    position_data["stats"].N += 1
                    sample_stats[sample_name].N += 1

                position_data[sample_name] = extended_code

            if args.reference:
                continue

            if args.column_Ns and position_data["stats"].N / len(args.input) > args.column_Ns:
                avail_pos[record.CHROM].remove(record.POS)

                # print "excluding %s" % record.POS
                if record.CHROM not in exclude:
                    exclude[record.CHROM] = FastRBTree()
                exclude[record.CHROM].insert(record.POS, False)

            if args.column_gaps and position_data["stats"].gap / len(args.input) > args.column_gaps:
                avail_pos[record.CHROM].remove(record.POS)

                if record.CHROM not in exclude:
                    exclude[record.CHROM] = FastRBTree()
                exclude[record.CHROM].insert(record.POS, False)

#             if not args.local:
#                 vcf_data[vcf_in].append(record)
#
#             if record.CHROM not in contigs:
#                 contigs.append(record.CHROM)
#                 avail_pos[record.CHROM] = FastRBTree()
#                 mixtures[record.CHROM] = {}
#                 sample_stats[record.CHROM] = {}
#
#             if sample_name not in mixtures[record.CHROM]:
#                 mixtures[record.CHROM][sample_name] = FastRBTree()
#
#             if sample_name not in sample_stats[record.CHROM]:
#                 sample_stats[record.CHROM][sample_name] = {}
#
#             if not record.FILTER:
#                 if record.is_snp:
#                     if record.POS in avail_pos[record.CHROM] and avail_pos[record.CHROM][record.POS] != record.REF:
#                         logging.critical("SOMETHING IS REALLY WRONG because reference for the same position is DIFFERENT! %s in %s", record.POS, vcf_in)
#                         return 2
#
#                     if record.CHROM not in pos_stats:
#                         pos_stats[record.CHROM] = {}
#
#                     avail_pos[record.CHROM].insert(record.POS, str(record.REF))
#                     pos_stats[record.CHROM][record.POS] = {"N":0, "-": 0, "mut": 0, "mix": 0, "gap": 0}
#
#             elif args.with_mixtures and record.is_snp:
#                 mix = get_mixture(record, args.with_mixtures)
#
#                 for ratio, code in mix.items():
#                     for c in code:
#                         avail_pos[record.CHROM].insert(record.POS, str(record.REF))
#                         if record.CHROM not in pos_stats:
#                             pos_stats[record.CHROM] = {}
#                         pos_stats[record.CHROM][record.POS] = {"N": 0, "-": 0, "mut": 0, "mix": 0, "gap": 0}
#
#                         if sample_name not in mixtures[record.CHROM]:
#                             mixtures[record.CHROM][sample_name] = FastRBTree()
#
#                         mixtures[record.CHROM][sample_name].insert(record.POS, c)
#             elif not record.is_deletion and not record.is_indel:
#                 if record.CHROM not in pos_stats:
#                     pos_stats[record.CHROM] = {}
#                 pos_stats[record.CHROM][record.POS] = {"N": 0, "-": 0, "mut": 0, "mix": 0, "gap": 0}
#                 avail_pos[record.CHROM].insert(record.POS, str(record.REF))
#             else:
#                 logging.debug("Discarding %s from %s as DEL and/or INDEL", record.POS, vcf_in)
#                 indel_summary[vcf_in] += 1
#                 try:
#                     vcf_data[vcf_in].remove(record)
#                 except ValueError:
#                     pass
#
#
#     all_data = { contig: {} for contig in contigs}
#     samples = []
#
#     for vcf_in in args.input:
#
#         sample_seq = ""
#         sample_name, _ = os.path.splitext(os.path.basename(vcf_in))
#         samples.append(sample_name)
#
#         # Initialise the data for this sample to be REF positions.
#         for contig in contigs:
#             all_data[contig][sample_name] = { pos: avail_pos[contig][pos] for pos in avail_pos[contig] }
#
#         # Re-read data from VCF if local is specified, otherwise get it from memory.
#         iterator = vcf.Reader(filename=vcf_in) if args.local else vcf_data[vcf_in]
#         for record in iterator:
#             # Array of filters that have been applied.
#             filters = []
#
#             # If position is our available position.
#             if avail_pos.get(record.CHROM, empty_tree).get(record.POS, False):
#                 if not record.FILTER:
#                     if record.is_snp:
#                         if len(record.ALT) > 1:
#                             logging.info("POS %s passed filters but has multiple alleles. Inserting N")
#                             all_data[record.CHROM][sample_name][record.POS] = "N"
#                         else:
#                             all_data[record.CHROM][sample_name][record.POS] = record.ALT[0].sequence
#                             pos_stats[record.CHROM][record.POS]["mut"] += 1
#                 else:
#
#                     # Currently we are only using first filter to call consensus.
#                     extended_code = mixtures[record.CHROM][sample_name].get(record.POS, "N")
#
# #                     extended_code = PHEFilterBase.call_concensus(record)
#
#                     # Calculate the stats
#                     if extended_code == "N":
#                         pos_stats[record.CHROM][record.POS]["N"] += 1
#
#                         if "n_pos" not in sample_stats[record.CHROM][sample_name]:
#                             sample_stats[record.CHROM][sample_name]["n_pos"] = []
#                         sample_stats[record.CHROM][sample_name]["n_pos"].append(record.POS)
#
#                     elif extended_code == "-":
#                         pos_stats[record.CHROM][record.POS]["-"] += 1
#                     else:
#                         pos_stats[record.CHROM][record.POS]["mix"] += 1
# #                         print "Good mixture %s: %i (%s)" % (sample_name, record.POS, extended_code)
#                     # Record if there was uncallable genoty/gap in the data.
#                     if record.samples[0].data.GT == "./.":
#                         pos_stats[record.CHROM][record.POS]["gap"] += 1
#
#                     # Save the extended code of the SNP.
#                     all_data[record.CHROM][sample_name][record.POS] = extended_code
#         del vcf_data[vcf_in]

    # Output the data to the fasta file.
    # The data is already aligned so simply output it.
    discarded = {}

    if args.sample_Ns:
        ss = []
        for sample_name in samples:
            total_positions = 0
            for contig in contigs:
                total_positions += len(avail_pos[contig])
            if sample_stats[sample_name].N / total_positions <= args.sample_Ns:
                ss.append(sample_name)
            else:
                print "EXCLUDING: %s for high N fraction: %s" % (sample_name, sample_stats[sample_name].N / total_positions)

        samples = ss

    samples.append("reference")

#     if args.reference:
#         # These should be in the same order as the order in reference.
#         contigs = args.reference.keys()

#     if args.sample_Ns:
#         delete_samples = []
#         for contig in contigs:
#             for sample in samples:
#
#                 # Skip if the contig not in sample_stats
#                 if contig not in sample_stats:
#                     continue
#
#                 sample_n_ratio = float(len(sample_stats[contig][sample]["n_pos"])) / len(avail_pos[contig])
#                 if sample_n_ratio > args.sample_Ns:
#                     for pos in sample_stats[contig][sample]["n_pos"]:
#                         pos_stats[contig][pos]["N"] -= 1
#
#                     logging.info("Removing %s due to high Ns in sample: %s", sample , sample_n_ratio)
#
#                     delete_samples.append(sample)
#
#         samples = [sample for sample in samples if sample not in delete_samples]
    snp_positions = []
    sample_seqs = { sample_name: "" for sample_name in samples }
    c = 0
    for contig in contigs:

        if contig not in avail_pos:
            for sample in samples:
                sample_seqs[sample] += args.reference[contig]
            continue

        if args.reference:
            positions = xrange(1, len(args.reference[contig]) + 1)
        else:
            positions = avail_pos[contig]

        for pos in positions:
            c += 1

            if args.reference and pos not in avail_pos[contig]:
                # reference base for everything.
                for sample in samples:
                    sample_seqs[sample] += args.reference[contig][pos - 1]
                continue

            for sample in samples:
                ref_base = avail_pos[contig][pos].get("reference")
                try:
                    sample_seqs[sample] += avail_pos[contig][pos].get(sample, ref_base)
                except TypeError:
                    print "%s" % sample_seqs[sample]
                    print "%s,%s,%s, %s" % (pos, sample, ref_base, avail_pos[contig][pos].get(sample, ref_base))
                    exit()

    with open(args.out, "w") as fp:
        for sample in sample_seqs:
            print "%s: %i" % (sample, len(sample_seqs[sample]))
            fp.write(">%s\n%s\n" % (sample, sample_seqs[sample]))

    for sample in sample_stats:
        total_positions = 0
        for contig in contigs:
            total_positions += len(avail_pos[contig])
        sample_stats[sample].total = total_positions
        print "%s\t%s" % (sample, str(sample_stats[sample]))

    if can_stats and args.with_stats:
        with open(args.with_stats, "wb") as fp:
            fp.write("contig,position,mutations,n_frac,n_gaps\n")
            for contig in contigs:
                for pos in avail_pos[contig]:
                    position_data = avail_pos[contig][pos]
                    fp.write("%s,%i,%0.5f,%0.5f,%0.5f\n" % (contig,
                                                 pos,
                                                 float(position_data["stats"].mut) / len(args.input),
                                                 float(position_data["stats"].N) / len(args.input),
                                                 float(position_data["stats"].gap) / len(args.input))
                             )
        plot_stats(avail_pos, len(samples) - 1, discarded=discarded, plots_dir=os.path.abspath(args.plots_dir))


#                 if contig in avail_pos:
#                     if args.reference:
#                         positions = xrange(1, len(args.reference[contig]) + 1)
#                     else:
#                         positions = avail_pos[contig].keys()
#                     for pos in positions:
#                         if pos in avail_pos[contig]:
#                             if not args.column_Ns or float(pos_stats[contig][pos]["N"]) / len(samples) < args.column_Ns and \
#                                 float(pos_stats[contig][pos]["-"]) / len(samples) < args.column_Ns:
#                                 sample_seq += all_data[contig][sample][pos]
#                             else:
#                                 if contig not in discarded:
#                                     discarded[contig] = []
#                                 discarded[contig].append(pos)
#                         elif args.reference:
#                             sample_seq += args.reference[contig][pos - 1]
#                 elif args.reference:
#                     sample_seq += args.reference[contig]
#
#             fp.write(">%s\n%s\n" % (sample, sample_seq))
#         # Do the same for reference data.
#         ref_snps = ""
#
#         for contig in contigs:
#             if contig in avail_pos:
#                 if args.reference:
#                     positions = xrange(1, len(args.reference[contig]) + 1)
#                 else:
#                     positions = avail_pos[contig].keys()
#                 for pos in positions:
#                     if pos in avail_pos[contig]:
#                         if not args.column_Ns or float(pos_stats[contig][pos]["N"]) / len(samples) < args.column_Ns and \
#                                 float(pos_stats[contig][pos]["-"]) / len(samples) < args.column_Ns:
#
#                             ref_snps += str(avail_pos[contig][pos])
#                             snp_positions.append((contig, pos,))
#                     elif args.reference:
#                         ref_snps += args.reference[contig][pos - 1]
#             elif args.reference:
#                     ref_snps += args.reference[contig]
#
#         fp.write(">reference\n%s\n" % ref_snps)
#
#     total_discarded = 0
#     for _, i in discarded.items():
#         total_discarded += len(i)
#     logging.info("Discarded total of %i poor quality columns", float(total_discarded) / len(args.input))
#     logging.info("Samples with indels:")
#     for sample, count in indel_summary.iteritems():
#         logging.info("%s\t%s", sample, count)
    return 0

if __name__ == '__main__':
    import time

#     with PyCallGraph(output=graphviz):
#     T0 = time.time()
    r = main()
#     T1 = time.time()

#     print "Time taken: %i" % (T1 - T0)
    exit(r)
