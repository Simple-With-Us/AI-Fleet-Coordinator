#!/usr/bin/env ruby
# frozen_string_literal: true

# Reject a workflow file GitHub would register under its path.
#
# GitHub's workflow parser refuses a mapping that repeats a key.  A parser
# that builds a Hash keeps the last value and hides the repeat, which is how
# codex-autofix-reusable.yml stayed red on every push with zero jobs (AFC-529).
# Psych's AST still holds both keys.  Ruby is already on the CI runner, so
# this check needs no extra package.  PyYAML is not installed in the test job
# and would collapse the same key.

require "psych"

def key_name(node)
  return nil unless node.is_a?(Psych::Nodes::Scalar)
  node.value
end

def line_of(node)
  node.respond_to?(:start_line) ? node.start_line + 1 : 0
end

def walk(node, dups)
  case node
  when Psych::Nodes::Stream, Psych::Nodes::Document, Psych::Nodes::Sequence
    node.children.each { |child| walk(child, dups) }
  when Psych::Nodes::Mapping
    seen = {}
    node.children.each_slice(2) do |key_node, value_node|
      name = key_name(key_node)
      if name
        if seen.key?(name)
          dups << [name, line_of(key_node), seen[name]]
        else
          seen[name] = line_of(key_node)
        end
      end
      walk(value_node, dups) if value_node
    end
  end
end

def scan(path)
  dups = []
  walk(Psych.parse_stream(File.read(path)), dups)
  dups
rescue Psych::SyntaxError => err
  raise "cannot parse #{path}: #{err.message}"
end

def files_under(dir)
  Dir.children(dir).sort.flat_map do |name|
    next [] unless name.end_with?(".yml", ".yaml")
    path = File.join(dir, name)
    File.file?(path) ? [path] : []
  end
end

if ARGV.empty?
  warn "usage: check-workflow-duplicate-keys.rb <workflow-dir-or-file>..."
  exit 2
end

failed = false
ARGV.each do |arg|
  paths = if File.directory?(arg)
            files_under(arg)
          else
            [arg]
          end
  if paths.empty?
    warn "no workflow YAML in #{arg}"
    failed = true
    next
  end
  paths.each do |path|
    begin
      dups = scan(path)
    rescue StandardError => err
      warn err.message
      failed = true
      next
    end
    dups.each do |name, line, first|
      warn "#{path}:#{line}: duplicate key #{name.inspect} (first at line #{first})"
      failed = true
    end
  end
end

exit(failed ? 1 : 0)
